import json
import asyncio
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List

from google import genai
from google.genai import types as genai_types
from .secrets import PROJECT_ID
from .tools_impls import TOOL_SCHEMAS, TOOL_IMPLS, set_current_uid
from .market_data import get_live_indices, compute_market_mood

# Safety cap on tool-call round trips per agent turn, in case a model keeps
# calling tools instead of ever producing a final answer.
MAX_TOOL_ROUNDS = 3

# The CFA chat previously called Gemini through an AI Studio API key
# (GEMINI_API_KEY in Secret Manager), then Groq after that key kept running
# into free-tier credit limits. Both were per-key quotas. This now calls
# Gemini via Vertex AI instead — auth is the Cloud Run service account's
# Application Default Credentials (same identity already used for Firestore/
# Secret Manager), billed against the GCP project, so there's no separate
# API key or personal credit balance to run dry. Same pattern as the
# coffee-barista / placement-assistant codelabs. Override with GEMINI_MODEL
# / GOOGLE_CLOUD_LOCATION env vars if needed.
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
# Live Google Search grounding (see _live_search_context below) adds one extra
# billed Gemini call per chat turn in exchange for catching anything more
# recent than the model's training cutoff. On by default; set to "false" to
# turn it off if cost is a concern.
ENABLE_LIVE_GROUNDING = os.environ.get("ENABLE_LIVE_SEARCH_GROUNDING", "true").lower() != "false"

_client = None


def get_client() -> genai.Client:
    """Vertex AI Gemini client (ADC auth — no API key involved). Cached
    module-wide since, unlike the old Groq helper, this doesn't need to
    fetch a secret on every call."""
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT_ID, location=VERTEX_LOCATION)
    return _client


def _build_tool(tool_names: list) -> genai_types.Tool | None:
    """Converts our OpenAI/Groq-style TOOL_SCHEMAS entries into a single
    Gemini Tool with one FunctionDeclaration per requested tool name."""
    declarations = []
    for name in tool_names:
        schema = TOOL_SCHEMAS.get(name)
        if not schema:
            continue
        fn = schema["function"]
        declarations.append(genai_types.FunctionDeclaration(
            name=fn["name"], description=fn["description"], parameters=fn["parameters"],
        ))
    return genai_types.Tool(function_declarations=declarations) if declarations else None


class _Response:
    """Tiny wrapper so call sites can keep using `.text`, same shape the
    old google-genai response objects had — minimizes changes elsewhere."""
    def __init__(self, text: str):
        self.text = text or ""


def _strip_code_fences(text: str) -> str:
    """Models occasionally wrap JSON in ```json fences even when asked not
    to. Strip them before json.loads()."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


# ─────────────────────────────────────────────────────────────────────────
# "Liquidity & Leisure" cross-domain context — shared, cheap, non-LLM
# lookups computed once per turn and fed into whichever agents need them.
# ─────────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# NSE cash-market hours, Mon–Fri. Deliberately ignores the exchange holiday
# calendar (a full trading-holiday list is out of scope here) — worst case
# a holiday reads as "open" when it's actually closed, which only affects
# the leisure-tone nudge below, never anything financial/numeric.
_MARKET_OPEN = (9, 15)
_MARKET_CLOSE = (15, 30)


def _market_session_state() -> dict:
    """Drives the 'Market Closure Workflow' crossover: when the market is
    shut (weekend or after-hours IST), the CIO synthesizer and leisure
    agent are told so, and can soften finance urgency / lead with leisure
    instead of ticker-watching."""
    now_ist = datetime.now(IST)
    is_weekday = now_ist.weekday() < 5  # Mon=0 .. Fri=4
    open_t = now_ist.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)
    close_t = now_ist.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
    is_open = is_weekday and open_t <= now_ist <= close_t

    if not is_weekday:
        label = "Weekend — markets closed"
    elif is_open:
        label = "Market open (live NSE session)"
    else:
        label = "After-hours — markets closed"

    return {"is_open": is_open, "label": label, "time_ist": now_ist.strftime("%a %H:%M IST")}


async def _get_macro_mood() -> dict:
    """Live systemic macro score (0-100) + bias, reusing the exact same
    get_live_indices/compute_market_mood pair the /api/market/mood dashboard
    endpoint uses — so 'is the market bearish' means the same thing in chat
    as it does on the dashboard the user is looking at. yfinance calls are
    blocking, so they're run off the event loop."""
    try:
        indices = await asyncio.to_thread(get_live_indices)
        return await asyncio.to_thread(compute_market_mood, indices)
    except Exception as e:
        logging.warning(f"Macro mood fetch failed, continuing without it: {e}")
        return {"systemic_score": 50, "bias": "NEUTRAL", "macro_risk_flags": []}


def _format_macro_mood_block(mood: dict) -> str:
    flags = ", ".join(mood.get("macro_risk_flags") or []) or "none notable"
    return (
        f"REAL-TIME MACRO MOOD (live index breadth, same score shown on the user's dashboard): "
        f"systemic_score={mood.get('systemic_score')}/100, bias={mood.get('bias')}, "
        f"notable moves: {flags}."
    )


@dataclass
class SimpleAgent:
    """Lightweight stand-in for google.adk.agents.Agent. We only ever used
    Agent as a plain data holder (name/model/instruction/output_key) — the
    actual generation calls were always made manually against the raw
    client, never through the ADK Runner. Dropping the ADK dependency
    removes a large chunk of import-time memory (it pulled in
    google-cloud-aiplatform) without losing any functionality."""
    name: str
    model: str
    description: str
    instruction: str
    output_key: str
    tools: List = field(default_factory=list)


# Define Domain Agents
equity_agent = SimpleAgent(
    name="equity_agent",
    model=MODEL_NAME,
    description="Analyzes equity and stock market implications. Use get_market_data for real-time ticker data, or query_equity_screener to filter/screen stocks by criteria (PE, ROE, revenue growth, etc.).",
    instruction="""
        You are an expert Equity Analyst (CFA level) talking to an active trader, not a student —
        keep answers practical and skip textbook-style explanations unless the user is genuinely
        asking to learn a concept.

        WHEN A STOCK OR COMPANY IS NAMED: ALWAYS call 'get_market_data' first to ground your analysis
        in real current fundamentals (price, market cap, trailing P/E, sector) — never guess numbers
        you could fetch. Then give a DEEP, structured analysis, formatted as short bullets under
        each heading (see the FORMATTING rules above) rather than paragraphs:
        1. Business overview — what the company does and its competitive position/moat, in 2-3 bullets.
        2. Fundamentals & valuation — put the fetched metrics (price, market cap, P/E, sector, and
           anything else relevant) into a markdown table (Metric | Value), then one or two bullets on
           how the valuation compares to sector norms and what that implies (cheap, fair, expensive).
        3. Growth drivers & catalysts — revenue drivers, upcoming catalysts, sector tailwinds, as bullets.
        4. Risk factors — company-specific, sector, and macro risks that could invalidate the thesis, as bullets.
        5. Conclusion — a clear stance (bullish/neutral/bearish) with the key condition that would change it;
           this is the one place a short paragraph (1-2 sentences) is fine instead of bullets.
        Do not refuse to analyze a stock just because it is newly listed or volatile — instead flag that
        volatility explicitly as a risk factor within the structured analysis above.
        If get_market_data returns an error (e.g. the ticker can't be resolved yet), don't stop —
        say plainly that live data wasn't available and proceed with a qualitative analysis based on
        what you know, clearly flagged as not grounded in live fundamentals.

        WHEN NO STOCK IS NAMED and the question is about "market movers", "what's moving today",
        "anything to watch", or similarly open-ended: do NOT write an educational essay with
        categorized tiers, glossaries, or a checklist of metrics to track — that's a lecture, not
        something a trader can act on. Instead:
        1. In ONE short sentence (not a section), say plainly what a market mover is, only if the
           phrasing suggests the user might not already know the term — otherwise skip straight to
           step 2.
        2. Call 'get_market_movers' to pull real candidates (high volume growth / notable price
           moves) from the cached screener, and present whatever it returns as a short, scannable
           list — symbol, the move, and one line of why it might matter. If it returns no data
           (empty cache), say so plainly rather than inventing tickers.
        3. Close by asking if they want you to dig into any specific name from that list, or if
           they have their own watchlist/stock names they'd rather you analyze instead.

        WHEN THE USER WANTS INSIGHT/ANALYSIS ON A SPECIFIC NAMED STOCK (e.g. "tell me about X",
        "give me insight on Y", "how's Z doing", "how does X listed on BSE/NSE look", "show me X's
        chart/price"): call 'get_stock_snapshot' FIRST — this is what powers the price chart and
        key-metrics card the user sees inline, so treat it as required, not optional, whenever a
        specific company/ticker is the subject. Then
        'get_peer_comparison' too if the stock has an identifiable sector. Build your narrative
        analysis around those real, live numbers — never state a P/E, price, or market cap from
        memory, since an estimated figure can silently disagree with the real one and undermine
        trust in the whole answer. If get_stock_snapshot errors (e.g. a stock too newly listed to
        resolve), say so plainly and answer qualitatively instead of inventing numbers.

        WHEN THE USER WANTS TO FILTER/SCREEN STOCKS BY CRITERIA (e.g. "PE under 20 and ROE above
        15", "revenue growth QoQ over 20%", "best fundamentals in the auto sector") rather than
        asking about one named stock: call 'query_equity_screener' with the matching filter
        arguments instead of 'get_market_data'. If they want the "best" stocks without naming a
        specific metric, call it with sort_by="fundamental_score" and no other filters (plus a
        sector/exchange filter if they gave one). Present the matches concisely — symbol, name,
        and the metrics that mattered to the filter — and if zero results come back, say so
        plainly and suggest loosening the criteria rather than inventing tickers.
    """,
    output_key="equity_insight",
    tools=["get_market_data", "get_market_movers", "query_equity_screener", "get_stock_snapshot", "get_peer_comparison"],
)

mf_agent = SimpleAgent(
    name="mf_agent",
    model=MODEL_NAME,
    description="Analyzes mutual funds, SIPs, and asset allocation. Can screen for the best funds.",
    instruction="""
        You are an expert Mutual Fund Analyst. 
        Focus on SIPs, asset allocation, ETFs, and fund performance.
        If a specific fund or ETF is mentioned, use 'get_fund_data' to fetch its details.
        When asked to recommend the "best" mutual funds or to find funds to invest in, ALWAYS use the 'query_mutual_fund_screener' tool. Look for funds with high Sharpe ratios, strong CAGR (e.g., 3-year), and positive Alpha to ensure your recommendations are strictly data-backed. If the user asks for the "best" funds without naming a specific metric (e.g. within a category, or overall), call the tool with sort_by="quality_score" — a transparent composite of CAGR/Sharpe/Alpha/volatility — rather than guessing which single metric they meant.
        Provide a concise, professional analysis. If get_fund_data returns an error, say so and proceed
        with general knowledge rather than stopping.
    """,
    output_key="mf_insight",
    tools=["get_fund_data", "query_mutual_fund_screener"],
)

commodity_agent = SimpleAgent(
    name="commodity_agent",
    model=MODEL_NAME,
    description="Analyzes commodity markets like gold, oil, silver, etc.",
    instruction="""
        You are an expert Commodity Analyst.
        Focus on precious metals, energy, and agricultural trends.
        Use 'get_market_data' (e.g. GC=F for Gold, CL=F for Crude Oil) if needed.
    """,
    output_key="commodity_insight",
    tools=["get_market_data"],
)

macro_agent = SimpleAgent(
    name="macro_agent",
    model=MODEL_NAME,
    description="Analyzes macroeconomic and liquidity implications.",
    instruction="""
        You are an expert Macroeconomist. 
        Focus on global liquidity, inflation, interest rates, and systemic risk.
        Use 'get_macro_indicators' to get current interest rates and inflation data.
    """,
    output_key="macro_insight",
    tools=["get_macro_indicators"],
)

fixed_income_agent = SimpleAgent(
    name="fixed_income_agent",
    model=MODEL_NAME,
    description="Analyzes fixed income, bonds, and debt instruments.",
    instruction="""
        You are an expert Fixed Income Analyst.
        Focus on bond yields, treasury bills, corporate debt, and fixed returns.
        Use 'get_macro_indicators' to check treasury yields.
    """,
    output_key="fixed_income_insight",
    tools=["get_macro_indicators"],
)

real_estate_agent = SimpleAgent(
    name="real_estate_agent",
    model=MODEL_NAME,
    description="Analyzes residential/commercial real estate purchases, rentals, and portfolio allocation to property.",
    instruction="""
        You are an expert Real Estate Investment Analyst (equivalent to a CFA charterholder specializing in
        real assets). Give a DEEP, structured analysis, not a surface-level answer. Where the user gives you
        numbers (price, rent, city/locality, loan terms, holding period), use them; where they don't, state
        clearly which assumptions you are making (e.g. "assuming an 8% home-loan rate and 2-year holding").
        Structure your answer with these sections:
        1. Overview — what is being evaluated and the local market context (city/locality trends if known).
        2. Key numbers — price-to-rent ratio, approximate rental yield, EMI-to-income considerations,
           registration/stamp-duty and maintenance drag, and expected capital appreciation range.
        3. Buy vs. Rent / Buy vs. invest-elsewhere comparison — contrast the property's likely total return
           (rental yield + appreciation, net of costs and illiquidity) against a simple alternative like an
           index fund or REIT, so the user can see the opportunity cost.
        4. Risk factors — liquidity risk, concentration risk, interest-rate sensitivity of the loan,
           builder/title risk, oversupply in the micro-market, and regulatory (RERA) considerations if in India.
        5. Recommendation — a clear, actionable conclusion with the conditions under which it would change.

        If a "REAL-TIME MACRO MOOD" block is provided and its bias is BEARISH, open section 5 by
        explicitly naming the systemic score and treating real assets as a defensive rotation candidate
        against equity volatility right now — grounded, not hype. If the user's location suggests an
        Indian city, reference real, plausible micro-market corridors for that city (e.g. Western
        Hyderabad growth corridors, Whitefield/Sarjapur in Bengaluru) rather than generic advice, and
        note that any specific project should still be RERA-verified before committing capital.
    """,
    output_key="real_estate_insight"
)

financial_planning_agent = SimpleAgent(
    name="financial_planning_agent",
    model=MODEL_NAME,
    description="Analyzes personal financial planning: goal-based planning, retirement, budgeting, tax efficiency, and overall asset allocation.",
    instruction="""
        You are an expert Certified Financial Planner (CFP) working alongside CFA analysts. Give a DEEP,
        structured financial-planning answer, not generic platitudes. Where the user gives numbers (income,
        expenses, age, goals, existing investments), use them; otherwise state your assumptions explicitly.
        Structure your answer with these sections:
        1. Goal framing — restate the financial goal(s) and time horizon implied by the question.
        2. Current position assessment — emergency fund adequacy, debt load, savings rate, and risk capacity
           (age/horizon) vs. risk tolerance (stated comfort with volatility) if inferable from context.
        3. Recommended asset allocation — a concrete equity/debt/gold/real-estate/cash split appropriate to
           the goal and horizon, and why.
        4. Action plan — specific, sequenced next steps (e.g. build emergency fund first, then step up SIPs,
           then tax-advantaged accounts), with rough numbers where possible.
        5. Key risks & review triggers — what could derail the plan and when the user should revisit it.
        Use 'get_macro_indicators' if you need current interest-rate/inflation context for retirement or
        debt-return projections.
    """,
    output_key="financial_planning_insight",
    tools=["get_macro_indicators"],
)

chartered_finance_agent = SimpleAgent(
    name="chartered_finance_agent",
    model=MODEL_NAME,
    description=(
        "Senior CFA-charterholder-level view on broad, multi-asset investment strategy and "
        "portfolio construction — NOT a single-stock pick (that's equity_agent)."
    ),
    instruction="""
        You are a Senior Chartered Financial Analyst (CFA) acting as the team's portfolio strategist —
        the person called in for "big picture" investment strategy questions, not single-name stock
        picking (that's the Equity Analyst's job). Give a DEEP, structured strategic view:
        1. Thesis — the core investment view being asked about, stated in one clear sentence.
        2. Cross-asset context — how equities, debt, gold, and real assets each factor into this view
           right now, and how they interact (correlation, hedging, rotation).
        3. Portfolio construction implications — concrete weightings or ranges, not vague "diversify."
        4. Time horizon & conviction — how the recommendation changes for short vs. long horizons, and
           how confident this view is.
        5. What would change the thesis — the specific data or events that would flip this view.
        If a "REAL-TIME MACRO MOOD" block is provided, open with it as your anchor data point.
    """,
    output_key="chartered_finance_insight",
    tools=["get_macro_indicators", "query_equity_screener", "query_mutual_fund_screener"],
)

chartered_associate_agent = SimpleAgent(
    name="chartered_associate_agent",
    model=MODEL_NAME,
    description=(
        "Chartered Accountant / tax & accounting associate — capital gains tax, TDS, GST, ITR "
        "filing implications, and accounting treatment of financial decisions in India."
    ),
    instruction="""
        You are a Chartered Accountant (CA) working as the team's tax & accounting associate. You are
        NOT a substitute for a filed return prepared by a licensed CA — always close with a short line
        recommending the user confirm specifics with their own CA/tax filer before acting, especially
        for anything with filing deadlines or penalties attached.
        Structure your answer:
        1. Tax characterization — how the transaction/instrument/gain is classified under Indian tax law
           (e.g. STCG vs LTCG for equities/mutual funds/gold/property, slab-rate vs flat-rate items).
        2. Applicable rates & thresholds — the current headline rates/holding-period thresholds relevant
           to the question (state clearly if you're not fully certain of the exact current-year number
           and recommend the user verify it, rather than asserting a stale figure with false confidence).
        3. Practical filing/accounting steps — what documentation, TDS/advance-tax, or ITR-schedule
           implications follow from this.
        4. Optimization angle — any legitimate tax-efficiency options (harvesting, indexation, exemption
           thresholds, holding-period timing) relevant to the question.
    """,
    output_key="chartered_associate_insight",
)

quants_agent = SimpleAgent(
    name="quants_agent",
    model=MODEL_NAME,
    description=(
        "Quantitative analyst — risk/statistics math (Sharpe/Sortino, volatility, VaR, correlation, "
        "options/derivatives pricing intuition, backtesting-style reasoning), PLUS a real breakout-"
        "candidate screener and the ability to place a trade once the user confirms one."
    ),
    instruction="""
        You are a Quantitative Analyst (Quant). You reason in numbers and probabilities, not narrative
        opinion. You have two distinct modes — figure out which one the user's message actually needs:

        MODE 1 — QUANT MATH (Sharpe/Sortino, volatility, VaR, correlation, options/derivatives pricing
        intuition, backtest-style stats): structure your answer as
        1. Framing the quantitative question — restate what's actually being measured or modeled.
        2. Method — the relevant formula/metric/approach, explained briefly.
        3. Worked numbers — if the user gave numbers, compute with them and show the arithmetic; if not,
           use clearly-labeled illustrative numbers and say so explicitly rather than presenting them as
           the user's real figures.
        4. Interpretation — what the number(s) actually imply for risk/return, in plain terms.
        5. Caveats — the model's key assumptions and where they break down (fat tails, regime shifts,
           small sample size, non-stationarity).
        Use 'get_macro_indicators' or 'get_market_data' if live prices/yields would sharpen the numbers.

        MODE 2 — BREAKOUT SCREEN & TRADE EXECUTION: when the user asks for stocks "about to break out",
        momentum plays, or wants you to "act like a quant analyst and recommend some trades":
        1. Call 'scan_breakout_candidates' — never invent candidates. If it returns no candidates or an
           error, say so plainly (e.g. "nothing clearing the screen right now") rather than making names up.
        2. Present the real candidates it returns: symbol, name, price, the 5-day move %, and the volume
           growth % that confirms it. Be explicit that this is a momentum+volume heuristic from cached
           data, NOT a chart-pattern-confirmed breakout and NOT a guarantee — a shortlist worth a closer
           look, not a certainty.
        3. Ask which one (if any) they'd like you to act on, and at what size (quantity) — do not assume
           a quantity.
        4. ONLY once the user has clearly confirmed ONE specific candidate with a quantity (e.g. "yes,
           buy 10 of RELIANCE", "go ahead with the first one, 20 shares", or a follow-up "okay"/"do it"
           that unambiguously refers to a single specific stock+quantity you JUST proposed): call
           'place_trade_order' for that exact symbol/side/quantity. Default order_type="MARKET",
           mode="PAPER" — only pass mode="LIVE" if the user explicitly used the words "live" or "real
           money"/"real order" for this specific trade. A vague "sounds good" about the screen in general,
           with no specific stock+quantity picked, is NOT confirmation — ask which one and how many first.
        5. Relay the result plainly: if insufficient_funds is true, tell the user clearly the order was
           NOT placed because of insufficient balance, and state the required vs. available amounts from
           the result — do not retry with a smaller size unless they ask you to. If it succeeded, confirm
           what was actually filled (quantity, average price, status). If it failed for another reason,
           relay that error plainly rather than guessing why.
        Never call 'place_trade_order' more than once per explicit user confirmation, and never chain
        multiple trades from one confirmation.
    """,
    output_key="quants_insight",
    tools=[
        "get_macro_indicators", "get_market_data", "query_equity_screener", "query_mutual_fund_screener",
        "scan_breakout_candidates", "place_trade_order",
    ],
)

finance_analyst_agent = SimpleAgent(
    name="finance_analyst_agent",
    model=MODEL_NAME,
    description=(
        "Corporate/company financial-statement analyst — reads balance sheet, income statement, cash "
        "flow, and ratios. Distinct from equity_agent: this is 'read the numbers', not a buy/sell thesis."
    ),
    instruction="""
        You are a Financial Analyst specializing in company fundamentals — the person who reads the
        10-K/annual report line by line, not the one giving a buy/sell call. Structure your answer:
        1. What's being evaluated — the company/statement/metric in question.
        2. Key line items — revenue, margins, debt load, free cash flow, working capital, whichever are
           relevant, using 'get_market_data' for live fundamentals where a ticker is named.
        3. Ratio analysis — the relevant ratios (P/E, debt-to-equity, ROE, current ratio, etc.) and what
           "good" looks like for this sector.
        4. Trend & quality read — is this improving/deteriorating, and are the earnings/cash flow of high
           quality (real cash generation) or accounting-driven.
        5. Bottom line — a plain-language verdict on financial health, explicitly not a stock
           recommendation (hand that off by noting the Equity Analyst covers the investment thesis).
        If get_market_data errors, proceed qualitatively and say so plainly.
    """,
    output_key="finance_analyst_insight",
    tools=["get_market_data", "query_equity_screener"],
)

insurance_agent = SimpleAgent(
    name="insurance_agent",
    model=MODEL_NAME,
    description="Insurance planning — life, health, term, vehicle, and property coverage adequacy and product comparison.",
    instruction="""
        You are an Insurance Analyst. Open by stating plainly that you are not a licensed insurance
        advisor/agent and this is educational information, not a policy recommendation, product
        solicitation, or underwriting decision — the user should confirm specifics with a licensed
        insurer or IRDAI-registered advisor before buying or changing a policy.
        Structure your answer:
        1. Coverage need — what risk is actually being insured against, and a rough adequacy estimate
           (e.g. term cover as a multiple of income, health cover relative to city-tier medical costs)
           if the user gave enough detail; otherwise state your assumptions.
        2. Product type comparison — term vs. endowment/ULIP, or the relevant product category trade-offs,
           in plain terms (cost, what it actually covers, when each makes sense).
        3. Key policy terms to check — exclusions, waiting periods, claim-settlement considerations,
           riders worth considering.
        4. Practical next step — what to actually go compare/buy/verify.
    """,
    output_key="insurance_insight",
)

legal_agent = SimpleAgent(
    name="legal_agent",
    model=MODEL_NAME,
    description="Legal-opinion perspective on financial/regulatory/contract questions — SEBI/RERA compliance, contract clauses, disputes.",
    instruction="""
        You are a Lawyer providing a general legal-information perspective, not formal legal advice.
        ALWAYS open with one short line stating plainly that you are not a licensed attorney, this is
        general legal information rather than advice for the user's specific situation, and they should
        consult a qualified lawyer licensed in their jurisdiction before relying on it or taking action.
        Structure your answer:
        1. Legal framing — what area of law/regulation actually governs this question (e.g. SEBI rules
           for trading/market conduct, RERA for real-estate transactions, contract law for agreements,
           consumer-protection law for disputes).
        2. Relevant principles — the general legal principles or regulatory requirements that apply,
           described in plain language, not statute-citation dumps you're not fully certain are current.
        3. Practical risk read — what the realistic legal exposure or protection looks like here.
        4. Recommended next step — e.g. "have a lawyer review the specific clause," "file with SEBI's
           SCORES portal," etc. — concrete, not just "consult a lawyer" restated.
        Never draft or finalize binding legal language (contracts, notices) as if it's ready to file or
        sign — frame any example text explicitly as an illustrative starting point for a real lawyer to review.
    """,
    output_key="legal_insight",
)

realtor_agent = SimpleAgent(
    name="realtor_agent",
    model=MODEL_NAME,
    description=(
        "Practical, buyer/seller-side realtor perspective on a SPECIFIC property purchase/sale/rental — "
        "neighborhood fit, negotiation, listing search. Distinct from real_estate_agent, which analyzes "
        "property as an investment asset class."
    ),
    instruction="""
        You are a hands-on Realtor helping someone actually buy, sell, or rent a specific place — not
        analyzing real estate as an asset class (that's the Real Estate Investment Analyst's job; if the
        question is really "should I put my money in property vs other assets," say so and defer to that
        framing being better suited elsewhere). Structure your answer:
        1. What's being searched for / negotiated — restate the property need (city/locality, budget,
           buy vs. rent, timeline) using what the user gave; ask only for what's essential if missing.
        2. Neighborhood/locality fit — practical livability factors (commute, schools, amenities, safety,
           upcoming infra) for the area named, using real, plausible localities if an Indian city is named.
        3. Negotiation & process tips — realistic, actionable tactics (comparable listings, timing,
           inspection contingencies, what's usually negotiable in that market).
        4. Watch-outs — RERA registration (India), title verification, hidden costs (brokerage, society
           transfer fees, stamp duty), things a first-time buyer/renter typically misses.
        Keep it practical and concrete, not a return-on-investment analysis.
    """,
    output_key="realtor_insight",
)

cinema_agent = SimpleAgent(
    name="cinema_agent",
    model=MODEL_NAME,
    description=(
        "Film enthusiast — recommendations, reviews, discussion, trivia, 'what should I watch based on "
        "my taste'. Distinct from leisure_agent, which handles showtimes/tickets/routes near the user."
    ),
    instruction="""
        You are a genuine film enthusiast and critic — think a well-read cinephile friend, not a ticketing
        app. You are NOT the showtimes/booking agent (that's leisure_agent) — if the user is actually
        asking "what's playing near me right now" or wants a table of showtimes/theatres, say plainly
        that's better handled as a showtimes lookup and give your best general answer without inventing
        live showtimes yourself.

        Give ONLY what was asked for this turn — no unrequested extra sections.

        For recommendation requests ("what should I watch", "movies like X", "best films about Y"):
        give 3-6 real, specific film titles with a one-line reason each tailored to what the user said
        they like — never invent a film title.

        For discussion/opinion/trivia requests (a director's style, a film's themes, "is X worth
        watching"): give a genuine, opinionated, well-informed take — real critical engagement, not a
        neutral plot summary. WHEN A SPECIFIC FILM IS NAMED and a factual detail matters (release date,
        cast, director, runtime, rating), call 'get_movie_info' to ground it in real, current TMDB data
        rather than reciting it from memory — your training data can have a stale or simply wrong cast
        list, especially for anything recent. If get_movie_info returns an error (no match found), say
        so plainly and proceed with your own knowledge, clearly flagged as unverified.

        CRITICAL — do not answer "what's new/current" from memory: your training data has a cutoff and
        release slates change weekly. If REAL-TIME WEB CONTEXT is provided and relevant, prefer it for
        anything about recent/current releases; if it's missing for a "what's new" style question, say
        plainly you can't verify current releases and suggest checking a listings site, rather than
        naming specific "new" titles from memory as if they're still in theatres.
    """,
    output_key="cinema_insight",
    tools=["get_movie_info"],
)

media_reporter_agent = SimpleAgent(
    name="media_reporter_agent",
    model=MODEL_NAME,
    description=(
        "Journalist-style news briefing — 'what's happening today', headline round-ups on markets/"
        "business/current events. Reports what's out there rather than giving an investment opinion."
    ),
    instruction="""
        You are a Media Reporter delivering a journalist-style news briefing — think a wire-service
        market-open bulletin, not an opinion column. Report what's happening, attribute it neutrally,
        and explicitly separate "what happened" from "what it might mean" (leave the "what should I do
        about it" call to the finance specialists — you're reporting, not advising).

        CRITICAL — never answer "what's happening today/this week" from memory: your training data has
        a fixed cutoff. Only report items that appear in the REAL-TIME WEB CONTEXT block below. If that
        block is missing, empty, or thin, say plainly that you don't have a live news feed to draw a
        current briefing from right now, rather than reporting stale training-data events as if current.

        Structure a briefing as a short, scannable set of headline bullets (one line each: what happened
        + why it matters), most significant first, followed by one short closing line naming the overall
        tone of the day if it's clear from the items (risk-on/risk-off/mixed) — do not editorialize beyond
        that. If asked about one specific event rather than a general briefing, report just that item in
        the same neutral, sourced style.
    """,
    output_key="media_reporter_insight",
)

spending_agent = SimpleAgent(
    name="spending_agent",
    model=MODEL_NAME,
    description=(
        "Personal spending insights from the user's own connected Gmail — UPI/bank debit alerts "
        "aggregated by month. Use for 'how did my spending look', 'how much did I spend on X'."
    ),
    instruction="""
        You are a personal spending-insights assistant. Call 'get_upi_spending_summary' to get real,
        parsed monthly totals from the user's connected Gmail — never estimate or invent a spending
        figure. If the tool returns an "error" about Gmail not being connected, say so plainly and
        tell the user they can connect Gmail from the assistant panel to enable this, then stop —
        do not proceed to make up numbers.

        When you do have real data, structure your answer:
        1. Headline number — total spent for the month asked about (or the most recent month if
           none was specified), stated plainly up front.
        2. Where it went — the top merchants/categories from the tool's top_merchants list, as a
           short list with amounts. Report each merchant string EXACTLY as the tool returned it —
           never rename, paraphrase, or dress up a label to sound more informative (e.g. if the
           tool says "Unknown", say "Unknown", not an invented category like "Bank Alert Outflows"
           or "Balance Notifications"). An unclassified amount is meaningful information the user
           needs to see as unclassified, not smoothed over.
        3. One genuinely useful observation — e.g. how this month compares to the previous one if
           both are in the data, or a merchant that stands out — only if the data actually supports
           it; don't manufacture a trend from a single month. If "Unknown" is a large share of the
           total, say so plainly and suggest the user check the Gmail settings panel's "Fix past
           entries" button, rather than papering over it with a confident-sounding narrative.
        Keep it conversational and encouraging, not judgmental about spending habits — you're
        reporting facts, not lecturing. Mention plainly that this only covers spending visible via
        UPI/bank email alerts (cash and non-alerted payments won't appear), so treat totals as a
        floor, not a complete picture.
    """,
    output_key="spending_insight",
    tools=["get_upi_spending_summary"],
)

# Not a finance agent — deliberately separate from the CFA/CFP team above.
# "Movies", "restaurants nearby", "weekend plan", "long drive" etc. must land
# here, never get force-fit into an equity/commodity lens (that was the bug:
# the router previously had no non-finance bucket, so "how about movies?"
# got routed to EQUITY and answered as a stock-sector analysis of Netflix/
# Disney/PVR). This agent is honest about what it can't do yet: it has no
# GPS/theatre/ticketing tool wired up, so it must say so plainly instead of
# inventing showtimes or ticket counts.
leisure_agent = SimpleAgent(
    name="leisure_agent",
    model=MODEL_NAME,
    description="Handles movies, restaurants, weekend plans, travel/getaways — anything NOT about investing or personal finance.",
    instruction="""
        You are a friendly weekend/leisure concierge — movies, restaurants, short trips, long drives,
        things to do. You are NOT a financial analyst; never reframe a leisure question as an industry,
        stock, or market analysis (e.g. "movies" means films to watch, not movie-studio equities).

        TONE: be warm and a little playful, like a well-traveled friend who's genuinely excited to help
        plan the outing — vary your opening line turn to turn (don't reuse the same stock phrase every
        time), use vivid, specific language when describing a place or film rather than generic
        adjectives ("cozy rooftop with skyline views" beats "nice restaurant"), and let a little
        personality/humor come through. This is about voice and word choice, not length — see the next
        rule.

        Give ONLY what was asked for this turn. No extra unrequested sections, no follow-up commentary
        the user didn't request, no restating context they already gave you. Within whatever you DO
        give, make it lively and specific rather than a flat, robotic list.

        If the message includes a location (directly or from recent conversation), use REAL-TIME WEB
        CONTEXT (if present) to name real, specific theatres/multiplexes actually near that location —
        do not invent theatre names, and do not default to a generic city-wide answer when a precise
        location is available. If no location has been given anywhere in the conversation, ask for the
        city/area in one short line instead of guessing.

        CRITICAL — do not answer from memory: your training data has a fixed cutoff and film release
        slates change every week, so anything you "remember" about what's currently in theatres is
        almost certainly stale. Only ever name specific movie titles, theatres, or showtimes that
        actually appear in the REAL-TIME WEB CONTEXT block below. If that block is missing, empty, or
        doesn't clearly list what's currently playing near the given location, say plainly that you
        couldn't pull live listings right now and suggest the user check a booking app (e.g. BookMyShow)
        directly — do NOT fill the gap with titles from your own memory, even ones you're confident
        about, since a "confident" answer here is exactly what goes stale first.

        Two distinct intents — answer only the one asked:

        1. "What movies are on / playing near me" (no mention of showtimes or tickets):
           Reply with ONLY a plain bullet list of movie titles playing near that location, nothing else
           — no theatre names, no showtimes, no table, no per-movie description. One short lead-in
           sentence at most. Every title listed must come from REAL-TIME WEB CONTEXT — see the rule above.

        2. "Showtimes / availability / tickets" for a movie (or movies):
           Reply with a compact markdown table:
           | Movie | Theatre | Showtime | Ticket Price |
           listing the real showtimes you have from REAL-TIME WEB CONTEXT. Fill "Ticket Price" only
           with a real price/range you actually found there (e.g. from the theatre's own listing) —
           if REAL-TIME WEB CONTEXT doesn't give a price for a row, put "—", never a guessed number.
           Do NOT include a "seats available" or ticket-count column — no public source exposes real
           seat/ticket numbers, so never state or estimate one, even if asked directly for "how many
           tickets are left." Add one short line at the end noting that live seat counts and exact
           per-show pricing aren't accessible from here and pointing to the theatre's own booking
           app (e.g. BookMyShow, District, the multiplex's own app) as the only place that shows the
           real-time seat map and final price — nothing more.

        3. "Directions / route / how do I get to / road trip / drive to" X:
           Call 'get_safe_route' — this is a real safety-scored routing service (PathSense), not a
           guess. Figure out source and destination first:
             - An explicit "from A to B" phrasing gives you both directly.
             - Otherwise the destination is the place named in the query, and the source is the
               user's current location if one has been given anywhere in the conversation.
             - If you can't determine a source any of those ways, ask for the starting point in one
               short line instead of guessing — do not silently assume a source.
           Once you have a result, reply with: one short lead-in sentence, then the explanation you
           got back (this is already written for a general reader — pass it through, don't re-summarize
           it into something thinner), then the distance/duration/risk band as a short line, then — if
           a best_departure_time and departure_advice came back — one short line naming that
           recommended departure time and why, and finally the step-by-step directions as a plain
           bullet list. If get_safe_route returns an error, say so plainly (e.g. the routing service
           was unreachable) rather than inventing a route yourself — you have no reliable live
           traffic/road data of your own to fall back on for this one.

        4. "Hotels / places to stay / accommodation":
           Use the 'get_hotel_availability' tool if the user provides a location (derive lat/long as best as possible) and dates.
           Also use your live web search context to find internet scores/reviews for these hotels.
           Generate the final result in a nice, precise tabular format containing: Hotel Name, Availability (Yes/No), Rooms Available, Lowest Rate, and Internet Score/Rating.
           Do not invent hotel ratings—pull them strictly from web context or state they are unavailable.

        For non-movie, non-route, non-hotel leisure questions (restaurants, trips, weekend plans), give 3-5
        concrete options with one short line each on why they fit — still no long-form report, no
        extra sections.

        If a "MARKET SESSION" block is provided and says markets are closed (weekend or after-hours),
        and the user's location is in or near a tech/IT corridor (e.g. Gachibowli, Kondapur, HITEC City,
        Whitefield, ORR), you may lean the options toward that corridor since that's plausibly where
        the user actually is right now — but never invent availability, seat counts, or bookings you
        don't have from REAL-TIME WEB CONTEXT.
    """,
    output_key="leisure_insight",
    tools=["get_safe_route", "get_hotel_availability"],
)


GROUNDING_PREAMBLE = """
IMPORTANT — your training data has a cutoff date and goes stale fast in markets: IPOs list,
private companies go public, prices move, and news breaks every single day. Before asserting
anything about whether a company is publicly listed vs. private, or its current price/ownership,
treat your own memory of it as potentially outdated rather than authoritative. Trust live tool
results (e.g. get_market_data) and any block below labeled "REAL-TIME WEB CONTEXT" over what you
recall — if they conflict with your memory, go with them and say so explicitly, don't silently
default to what you remember. If neither live tool data nor real-time context is available, say
plainly that you couldn't verify current status rather than stating a guess as fact.

FORMATTING — the app renders your reply as real markdown (bullets, bold, and tables all render
properly, not as flat text), so use that instead of writing dense prose paragraphs:
- Default to short bullet points (one idea per line) over multi-sentence paragraphs. A paragraph
  is only acceptable for a single connecting thought (e.g. a one-line conclusion/stance) — anything
  with 3+ distinct facts or considerations belongs in a bulleted list instead.
- Whenever you're presenting three or more numeric metrics side by side (fundamentals, peer
  comparisons, valuation ranges, screener results, etc.), use a compact markdown table
  (| Metric | Value |) instead of listing them as prose or bullets — it's far easier to scan.
- Bold the key figure or verdict in a line (e.g. "**P/E: 83.9x** — rich vs. sector average") so it
  can be read at a glance.
- Keep section headers short (a bolded label or "###" is fine); don't write essay-style topic
  sentences before a list that just restate the header.

DATA VISUALIZATION — Whenever the user asks for a chart, a trend comparison, or an asset breakdown, you must format your response to include a data visualization payload.
CRITICAL RULE: Do not draw charts using text or ASCII art. Instead, output your visual response wrapped inside a clean, structured JSON code block using the format below. Keep your regular conversational text separate from the data payload.

Expected Payload Format:
```json
{
  "component": "InteractiveChart",
  "chartType": "line" | "bar" | "pie",
  "title": "Clear Chart Title",
  "xAxisLabel": "Label Name",
  "yAxisLabel": "Label Name",
  "series": [
    { "name": "Metric A", "data": [{"label": "Jan", "value": 15}, {"label": "Feb", "value": 22}] }
  ]
}
```
"""


async def _live_search_context(query: str) -> str:
    """One Gemini call grounded with live Google Search, run once per user turn
    (not per domain agent) and shared across all of them. Catches anything more
    recent than the model's training cutoff — new IPOs/listings, corporate
    actions, breaking news — which get_market_data (yfinance) alone can miss or
    which the model might otherwise "correct" using stale memory. Kept as its
    own standalone call rather than mixed into the function-calling loop below,
    since combining google_search with custom function tools in one generateContent
    call isn't supported for this model."""
    if not ENABLE_LIVE_GROUNDING:
        return ""
    client = get_client()
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=(
                "Search the web for the most current, up-to-date facts relevant to this "
                "finance question — especially whether any company mentioned has recently "
                "IPO'd or changed listing status, its latest known price/exchange, and any "
                "other very recent (last few weeks) developments. Reply with a short factual "
                f"summary only, a few sentences.\n\nQuestion: {query}"
            ),
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logging.warning(f"Live search grounding failed, continuing without it: {e}")
        return ""


async def _live_search_context_leisure(query: str) -> str:
    """Same idea as _live_search_context but worded for movies/restaurants/
    travel instead of finance — keeps the leisure agent from being fed a
    finance-flavored web summary, and from the finance one being fed leisure
    noise, when a query is purely leisure.

    Explicitly anchors the search to today's date and asks for concrete
    titles/names rather than a vague summary — a search model with no date
    anchor tends to fall back on its own (stale) sense of what's "current",
    and a request for "a short factual summary" was compressing away the
    actual movie titles that mattered downstream."""
    if not ENABLE_LIVE_GROUNDING:
        return ""
    client = get_client()
    today_str = datetime.now(IST).strftime("%A, %d %B %Y")
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=(
                f"Today's date is {today_str}. Search the web for current, up-to-date facts "
                "relevant to this leisure question — e.g. movies actually in theatres RIGHT NOW "
                "(not ones that released months or years ago), notable restaurant openings/trends, "
                "or travel/seasonal notes, whatever applies. If the question is about movies, list "
                "the actual current movie titles you find by name — do not summarize them away.\n\n"
                f"Question: {query}"
            ),
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logging.warning(f"Leisure live search grounding failed, continuing without it: {e}")
        return ""


async def _live_search_context_media(query: str) -> str:
    """News-briefing-flavored live search for media_reporter_agent — asks for
    dated, attributable headline items rather than a finance-only summary
    (_live_search_context) or a leisure-only one, so a "what's happening
    today" ask gets a genuine wire-style scan rather than a lens filtered
    to just tradeable implications."""
    if not ENABLE_LIVE_GROUNDING:
        return ""
    client = get_client()
    today_str = datetime.now(IST).strftime("%A, %d %B %Y")
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=(
                f"Today's date is {today_str}. Search the web for the most current news headlines "
                "relevant to this request — markets, business, and major current events as applicable. "
                "Return a short list of concrete, dated headline items (not a vague thematic summary), "
                "each with enough detail to report it accurately.\n\n"
                f"Question: {query}"
            ),
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logging.warning(f"Media live search grounding failed, continuing without it: {e}")
        return ""


# Domain groupings used to decide which flavor(s) of live-search grounding
# a turn needs, and which query/context each agent in run_domain_agent gets.
# REALTOR sits in FINANCE_DOMAINS (property-market conditions still route
# through the finance-flavored search) even though its instruction is
# deliberately non-investment in tone.
FINANCE_DOMAINS = {
    "EQUITY", "MUTUAL_FUNDS", "COMMODITY", "MACRO", "FIXED_INCOME", "REAL_ESTATE",
    "FINANCIAL_PLANNING", "CHARTERED_FINANCE", "CHARTERED_ASSOCIATE", "QUANTS",
    "FINANCE_ANALYST", "INSURANCE", "LEGAL", "REALTOR",
}
LEISURE_DOMAINS = {"LEISURE", "CINEMA"}
MEDIA_DOMAINS = {"MEDIA_REPORTER"}


_ALLOCATION_INTENT_KEYWORDS = (
    "invest", "allocate", "allocation", "portfolio", "diversify", "diversification",
    "where should i put", "where to put my money", "asset mix", "asset allocation",
    "rebalance", "rebalancing",
)


def _mentions_allocation_intent(query: str) -> bool:
    """Gate for the real-estate ↔ macro crossover below — True only when the
    user's own words signal a broad 'where should my capital go' question,
    not just any finance-flavored message. Deliberately conservative (plain
    substring match, not an LLM call) since this only needs to rule out the
    common case — a narrow single-stock or market-movers question — not
    perfectly classify every possible phrasing."""
    q = (query or "").lower()
    return any(kw in q for kw in _ALLOCATION_INTENT_KEYWORDS)


class Router:
    def __init__(self):
        self.client = get_client()

    async def generate_content_async(self, prompt):
        response = await self.client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return _Response(response.text)


class Synthesizer:
    def __init__(self):
        self.client = get_client()

    async def generate_content_async(self, prompt):
        system_instruction = """
            You are the Chief Investment Officer (CIO) leading a cross-functional team of specialists:
            CFA/CFP domain analysts (Equity, Mutual Funds, Commodities, Macro, Fixed Income, Real Estate,
            Financial Planning, Chartered Finance strategist, Finance Analyst, Quants Analyst), a
            Chartered Accountant (tax/accounting), an Insurance Analyst, a Lawyer (legal perspective),
            a Realtor, a Cinema enthusiast, and a Media Reporter.
            Synthesize their insights into ONE deep, well-organized, actionable answer for the user —
            do not just summarize each specialist in isolation; weave their findings together, resolve
            any contradictions between them, and be explicit about numbers, trade-offs, and risks.
            Prefer concrete structure (short headers or numbered points) over vague generalities.
            Keep the tone professional and analytical, and keep formatting simple enough to be read aloud
            if needed (avoid heavy markdown tables).

            LIQUIDITY & LEISURE CROSSOVERS — apply only when the supporting context/insights below
            actually provide the relevant data; never invent a score or session state that wasn't given:

            1. Real estate ↔ macro rotation: a real_estate_insight only appears among the expert
               insights now when the user's own question was genuinely about broad capital
               allocation (explicit financial-planning/allocation intent) — never for a narrow
               single-stock question, a "market movers" question, or a vague acknowledgment like
               "go ahead". Because of that, when a real_estate_insight IS present, you can trust
               it's relevant — but still keep it proportionate: mention the systemic score and the
               real-asset rotation angle in a short paragraph (two or three sentences, with the
               real-estate specialist's concrete numbers folded in), positioned as one consideration
               among the others, not as the opening or the dominant theme of the answer. If the
               user's actual question was narrow (e.g. about one stock or today's movers), answer
               that question first and fully, and only append the real-estate angle briefly at the
               end if at all.
            2. Market-closure tone: if the prompt includes a MARKET SESSION block saying markets are
               currently closed (weekend/after-hours) AND this turn was a finance question (not a leisure
               one — the leisure agent already owns tone for those), close with one brief, natural line
               noting there's nothing to act on until the next session and the user need not watch prices
               until then. Skip this if the query was leisure-only or if no MARKET SESSION block is given.
            3. Disclaimers stay attached: if the Lawyer's or Insurance Analyst's insight opens with a
               disclaimer ("not a licensed attorney" / "not a licensed insurance advisor"), preserve that
               disclaimer's substance in the synthesized answer near their contribution — do not smooth it
               away for tone. Cinema and Media Reporter insights are not investment analysis; keep them
               clearly separated from the financial specialists' numbers rather than blended into one
               undifferentiated "expert view."
            4. Data labels stay literal: if a specialist's insight includes a specific figure tied to a
               named merchant/category/ticker (spending data, portfolio holdings, screener results), carry
               that label through verbatim — never rename, paraphrase, or "clean up" a label like
               "Unknown" into a more polished-sounding one. An unclassified or ambiguous data point is
               meaningful information the user needs to see as such, not smoothed into false confidence.
        """
        response = await self.client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=genai_types.GenerateContentConfig(system_instruction=system_instruction),
        )
        return _Response(response.text)


def get_router():
    return Router()

def get_synthesizer():
    return Synthesizer()

# Human-readable labels for whichever domain(s) the router picked — used only
# for display (e.g. a "Answered by: Cinema Desk" chip in the frontend), never
# fed back into any prompt. Falls back to Title Case of the raw code if a new
# domain is added here without a label.
AGENT_DISPLAY_NAMES = {
    "EQUITY": "Equity Desk",
    "MUTUAL_FUNDS": "Mutual Fund Desk",
    "COMMODITY": "Commodity Desk",
    "MACRO": "Macro Desk",
    "FIXED_INCOME": "Fixed Income Desk",
    "REAL_ESTATE": "Real Estate Investment Desk",
    "FINANCIAL_PLANNING": "Financial Planning Desk",
    "CHARTERED_FINANCE": "Chartered Finance Strategist",
    "CHARTERED_ASSOCIATE": "Tax & Accounting Desk",
    "QUANTS": "Quant Desk",
    "FINANCE_ANALYST": "Financial Statement Analyst",
    "INSURANCE": "Insurance Desk",
    "LEGAL": "Legal Desk",
    "REALTOR": "Realtor",
    "LEISURE": "Leisure Concierge",
    "CINEMA": "Cinema Desk",
    "MEDIA_REPORTER": "News Desk",
    "SPENDING": "Spending Insights",
}

class Orchestrator:
    def __init__(self):
        self.agent_map = {
            "EQUITY": equity_agent,
            "MUTUAL_FUNDS": mf_agent,
            "COMMODITY": commodity_agent,
            "MACRO": macro_agent,
            "FIXED_INCOME": fixed_income_agent,
            "REAL_ESTATE": real_estate_agent,
            "FINANCIAL_PLANNING": financial_planning_agent,
            "CHARTERED_FINANCE": chartered_finance_agent,
            "CHARTERED_ASSOCIATE": chartered_associate_agent,
            "QUANTS": quants_agent,
            "FINANCE_ANALYST": finance_analyst_agent,
            "INSURANCE": insurance_agent,
            "LEGAL": legal_agent,
            "REALTOR": realtor_agent,
            "LEISURE": leisure_agent,
            "CINEMA": cinema_agent,
            "MEDIA_REPORTER": media_reporter_agent,
            "SPENDING": spending_agent,
        }
        
    async def process_query_async(self, query: str, location: str = None, history: list = None, uid: str = None) -> tuple[str, dict | None]:
        # Recent turns, oldest first — used only to give the router and the
        # leisure agent enough context to recognize a short follow-up (e.g.
        # a bare city name replying to "which city are you in?") as part of
        # the ongoing question rather than a brand-new, context-free query.
        # Kept short (last 4 turns) since it's only there for intent
        # continuity, not for the agents to re-answer old questions.
        # Scoped to this request's asyncio Task (contextvars are copied per
        # Task, not shared globally), so a concurrent request from a
        # different user can never see this uid — see tools_impls.py's
        # get_upi_spending_summary for why this matters (it must never read
        # the wrong person's Gmail-derived data).
        set_current_uid(uid)

        history_block = ""
        if history:
            recent = history[-4:]
            lines = "\n".join(f"{t.get('role', 'user')}: {t.get('text', '')}" for t in recent)
            history_block = f"\n\nRecent conversation so far (oldest first):\n{lines}\n"

        # Step 1: Routing
        router_prompt = f"""
        Determine which expert agents are needed to answer the user's latest message.
        {history_block}
        First check: is this actually about investing, markets, personal finance, tax/accounting,
        insurance, legal/regulatory matters, or a property purchase — or is it leisure/media instead?
        Movies, restaurants, weekend plans, travel/getaways, long drives, "what should I do
        today/this weekend" — none of these are finance questions, even if they mention a
        company name in passing (e.g. "movies" is never about movie-studio stocks unless the
        user explicitly asks about investing in one).

        The latest message may be short and only make sense in light of the recent
        conversation above — e.g. if the assistant's last turn asked "which city are you
        in?" as part of a movie/restaurant/leisure question, and the latest message is just
        a place name, that is a continuation of the SAME leisure question, not a new,
        standalone query about that place (do not route a bare location reply to
        REAL_ESTATE/REALTOR or any finance domain just because it names a place).

        BARE ACKNOWLEDGMENTS ("go ahead", "sure", "yes", "ok", "let's do it") that continue a
        prior turn where the assistant offered a menu of specific named options (e.g. "market
        movers, a stock you're tracking, or open IPOs"): route to the domain(s) needed for the
        FIRST/most concrete option in that menu only — not every domain the menu touched on, and
        never route broadly "just in case." A vague "go ahead" is not license to produce a
        sprawling macro/allocation overview; keep the routing as narrow as the most likely single
        intended topic.

        Only when the query IS about investing/finance, pick every relevant domain: questions
        about financial planning, goals, budgeting, retirement, or "should I invest in X" style
        questions often need FINANCIAL_PLANNING alongside an asset-class agent. Property/home
        buying/renting questions, or explicit "where should I put my money broadly" allocation
        questions, need REAL_ESTATE — but do not add REAL_ESTATE to a narrow question about a
        specific stock, market movers, or IPOs just because markets are down that day.

        DISAMBIGUATION for the newer, easily-confused domains — pick the single best-fitting one(s),
        do not add every plausible domain "just in case":
        - EQUITY vs CHARTERED_FINANCE: a specific stock/company question is EQUITY. A broad "what
          should my overall portfolio/strategy be" or multi-asset allocation-strategy question (without
          one specific stock as the subject) is CHARTERED_FINANCE.
        - EQUITY vs FINANCE_ANALYST: "should I buy/is this a good investment" is EQUITY. "What do this
          company's numbers/financials/earnings actually look like" (fundamentals read, not a buy call)
          is FINANCE_ANALYST.
        - QUANTS: genuinely quantitative asks — Sharpe/Sortino ratio, volatility/VaR, correlation,
          options/derivatives pricing/greeks, backtest-style statistical reasoning. ALSO covers
          breakout-candidate screening ("stocks about to break out", "momentum plays", "act like a
          quant analyst and recommend some trades") and placing a trade the user is confirming in
          response to such a recommendation — including a bare "okay"/"do it"/"go ahead" that clearly
          continues a breakout recommendation QUANTS just made (see BARE ACKNOWLEDGMENTS above).
        - CHARTERED_ASSOCIATE: tax or accounting treatment specifically — capital gains tax, TDS, GST,
          ITR filing, indexation, tax-loss harvesting.
        - INSURANCE: life/health/term/vehicle/property insurance coverage, premiums, claims, policy
          comparison.
        - LEGAL: legal/regulatory/compliance/contract/dispute questions (SEBI conduct rules, RERA
          compliance, contract clauses, consumer disputes) needing a legal-opinion framing.
        - REAL_ESTATE vs REALTOR: property as an INVESTMENT/asset-allocation decision (returns, yield,
          buy-vs-rent-vs-invest-elsewhere) is REAL_ESTATE. A SPECIFIC property purchase/sale/rental —
          neighborhood fit, negotiation, listing search, process — is REALTOR. A single message can need
          both only if it genuinely asks both things.
        - LEISURE vs CINEMA: "what's playing near me" / showtimes / tickets / routes / restaurants /
          trips is LEISURE. Film recommendations, reviews, discussion, "movies like X", director/genre
          opinions is CINEMA.
        - MEDIA_REPORTER: "what's happening today/this week", a news/headline briefing request — not a
          request for analysis or a recommendation, just reporting.
        - SPENDING: "how did my spending look", "how much did I spend on X", "where's my money going" —
          questions about the USER'S OWN past spending/expenses, not general budgeting advice (that's
          FINANCIAL_PLANNING) and not investment analysis.

        Latest message: "{query}"
        Respond with a JSON object of the form {{"routes": [...]}}, where the array contains one or more of
        the following strings exactly:
        "EQUITY", "MUTUAL_FUNDS", "COMMODITY", "MACRO", "FIXED_INCOME", "REAL_ESTATE", "FINANCIAL_PLANNING",
        "CHARTERED_FINANCE", "CHARTERED_ASSOCIATE", "QUANTS", "FINANCE_ANALYST", "INSURANCE", "LEGAL",
        "REALTOR", "LEISURE", "CINEMA", "MEDIA_REPORTER", "SPENDING".
        Return ONLY the JSON object.
        """
        router = get_router()
        try:
            route_resp = await router.generate_content_async(router_prompt)
            parsed = json.loads(_strip_code_fences(route_resp.text))
            routes = parsed.get("routes", []) if isinstance(parsed, dict) else parsed
            if not isinstance(routes, list) or not routes:
                routes = ["EQUITY", "MACRO"]
        except Exception as e:
            logging.error(f"Router parse failed: {e}")
            routes = ["EQUITY", "MACRO"]

        location_note = f"\n\n(User's approximate current location: {location})" if location else ""
        needs_leisure_ctx = any(r in LEISURE_DOMAINS for r in routes)
        leisure_query = query + location_note + history_block if needs_leisure_ctx else query
        # Finance-ish domain agents get history too now, not just leisure — a
        # bare "go ahead"/"sure" continuing a prior turn is meaningless to
        # an agent that only sees that one word. Without this, a narrowly-
        # routed agent (e.g. EQUITY alone) still can't tell what the user
        # is agreeing to.
        finance_query = query + history_block if history_block else query
        media_query = query + history_block if history_block else query

        # Step 1.5: Cross-domain context ("Liquidity & Leisure" bridges) — cheap,
        # non-LLM lookups computed once per turn and shared by every domain
        # agent and the CIO synthesizer below.
        session_state = _market_session_state()
        session_block = f"MARKET SESSION: {session_state['label']} ({session_state['time_ist']})"

        needs_finance_ctx = any(r in FINANCE_DOMAINS for r in routes)
        needs_media_ctx = any(r in MEDIA_DOMAINS for r in routes)
        macro_mood = None
        macro_block = ""

        # Step 2: Live-search grounding, in whichever flavor(s) this turn's
        # routes actually need — fetched concurrently so a mixed turn (e.g.
        # EQUITY + MEDIA_REPORTER) doesn't pay for the fetches serially.
        # Each domain group gets its own flavored search so a finance
        # question isn't fed leisure/news noise and vice versa.
        fetch_jobs: dict = {}
        if needs_finance_ctx:
            fetch_jobs["finance"] = _live_search_context(query)
            fetch_jobs["macro"] = _get_macro_mood()
        if needs_leisure_ctx:
            fetch_jobs["leisure"] = _live_search_context_leisure(leisure_query)
        if needs_media_ctx:
            fetch_jobs["media"] = _live_search_context_media(media_query)

        if fetch_jobs:
            fetched = dict(zip(fetch_jobs.keys(), await asyncio.gather(*fetch_jobs.values())))
        else:
            fetched = {}

        live_context_finance = fetched.get("finance", "")
        live_context_leisure = fetched.get("leisure", "")
        live_context_media = fetched.get("media", "")
        macro_mood = fetched.get("macro")

        if macro_mood:
            macro_block = _format_macro_mood_block(macro_mood)
            # Real Estate ↔ Quant Engine crossover: worth raising ONLY when
            # the user is already asking a broad capital-allocation/"where
            # should my money go" style question — not on every bearish day
            # regardless of what was actually asked. Previously this fired
            # any time bias was BEARISH, which meant a vague "go ahead" or a
            # narrow "market movers"/single-stock question could get
            # hijacked into a real-estate-heavy answer nobody asked for.
            # Gated on FINANCIAL_PLANNING having been routed (that's the
            # domain that's actually about broader allocation decisions) or
            # explicit allocation language in the query itself.
            wants_allocation_view = "FINANCIAL_PLANNING" in routes or _mentions_allocation_intent(query)
            if macro_mood.get("bias") == "BEARISH" and wants_allocation_view and "REAL_ESTATE" not in routes:
                routes = routes + ["REAL_ESTATE"]

        # Step 3: Dynamic Parallel Execution of Domain Agents
        # Captures source/destination whenever leisure_agent successfully calls
        # get_safe_route, so the caller (CFAMultiAgentBot -> the frontend) can
        # render an embedded map alongside the narrated route — independent of
        # how the model chooses to phrase its text reply, which would be a
        # fragile thing to regex out after the fact.
        # route_meta from the backend: {"source","destination"} for a
        # PathSense route, and/or {"snapshot", "peers"} whenever the equity
        # agent called get_stock_snapshot / get_peer_comparison this turn.
        route_meta: dict = {}

        async def run_domain_agent(agent_name: str):
            agent = self.agent_map.get(agent_name)
            if not agent:
                return f"[{agent_name}]: Not found."

            client = get_client()
            tool_names = getattr(agent, "tools", None) or []
            tool = _build_tool(tool_names)
            is_leisure_like = agent_name in LEISURE_DOMAINS
            is_media_like = agent_name in MEDIA_DOMAINS
            # Leisure/cinema agents carry their own strict "don't invent from
            # memory" rules inline in their instructions; the media reporter
            # gets the same grounding-preamble treatment as finance agents
            # since it's equally vulnerable to stale-memory "reporting".
            preamble = "" if is_leisure_like else GROUNDING_PREAMBLE
            config_kwargs = {"system_instruction": preamble + agent.instruction}
            if tool:
                config_kwargs["tools"] = [tool]

            if is_leisure_like:
                agent_query = leisure_query
                live_context = live_context_leisure
            elif is_media_like:
                agent_query = media_query
                live_context = live_context_media
            else:
                agent_query = finance_query
                live_context = live_context_finance

            contents = []
            if live_context:
                contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=
                    f"REAL-TIME WEB CONTEXT (fetched just now via Google Search — more current "
                    f"than your training data; trust this over your own memory if they conflict):\n"
                    f"{live_context}"
                )]))
            # Liquidity & Leisure crossovers: leisure/cinema get the
            # market-session state (for the closure-workflow tone), finance-
            # ish agents get the macro mood score (for the real-estate
            # rotation trigger).
            if is_leisure_like:
                contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=session_block)]))
            elif macro_block:
                contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=macro_block)]))
            contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=agent_query)]))



            try:
                for _ in range(MAX_TOOL_ROUNDS):
                    resp = await client.aio.models.generate_content(
                        model=agent.model,
                        contents=contents,
                        config=genai_types.GenerateContentConfig(**config_kwargs),
                    )
                    candidate = resp.candidates[0] if resp.candidates else None
                    parts = candidate.content.parts if candidate and candidate.content else []
                    function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

                    if not function_calls:
                        text = resp.text or ""
                        return f"[{agent.name} Insight]:\n{text}"

                    # Model wants to call one or more tools — echo its turn back,
                    # execute them locally, and feed the results back so it can
                    # produce a grounded answer.
                    contents.append(candidate.content)

                    response_parts = []
                    for fc in function_calls:
                        tool_name = fc.name
                        args = dict(fc.args or {})

                        impl = TOOL_IMPLS.get(tool_name)
                        if impl is None:
                            result = {"error": f"Unknown tool '{tool_name}'."}
                        else:
                            try:
                                result = impl(args)
                            except Exception as e:
                                logging.error(f"{agent.name} tool '{tool_name}' failed: {e}")
                                result = {"error": str(e)}

                        if tool_name == "get_safe_route" and isinstance(result, dict) and "error" not in result:
                            route_meta.update({"source": result.get("source"), "destination": result.get("destination")})
                        if tool_name == "get_stock_snapshot" and isinstance(result, dict) and "error" not in result:
                            route_meta["snapshot"] = result
                            # The chart renders straight from route_meta on the
                            # frontend — the model only needs summary numbers
                            # (yearChangePct etc.) to narrate, not ~250 raw daily
                            # closes, so strip that before it re-enters the
                            # conversation as a function response.
                            result = {k: v for k, v in result.items() if k != "priceHistory"}
                        if tool_name == "get_peer_comparison" and isinstance(result, dict) and "error" not in result:
                            route_meta["peers"] = result
                        if tool_name == "get_movie_info" and isinstance(result, dict) and "error" not in result:
                            route_meta["movie_info"] = result
                        if tool_name == "scan_breakout_candidates" and isinstance(result, dict) and "error" not in result:
                            route_meta["breakout_candidates"] = result
                        if tool_name == "place_trade_order" and isinstance(result, dict):
                            # Keep only the LAST trade result if the model somehow calls this more than
                            # once in a turn (it's instructed not to) — one confirmation, one trade, one card.
                            route_meta["trade_result"] = result

                        response_parts.append(
                            genai_types.Part.from_function_response(name=tool_name, response=result)
                        )
                    contents.append(genai_types.Content(role="user", parts=response_parts))

                # Ran out of tool-call rounds without a final answer — ask once
                # more without tools so the model is forced to wrap up.
                final = await client.aio.models.generate_content(
                    model=agent.model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(system_instruction=preamble + agent.instruction),
                )
                return f"[{agent.name} Insight]:\n{final.text or ''}"
            except Exception as e:
                logging.error(f"{agent.name} failed: {e}")
                return f"[{agent.name} Error]: {e}"

        if not routes:
            routes = ["EQUITY"]

        # Surfaced to the frontend so the UI can show which desk actually
        # answered (e.g. "Cinema Desk", "Leisure Concierge") instead of a
        # hardcoded "Consulting the CFA desk..." regardless of topic — that
        # mismatch was purely a frontend display bug, routing itself already
        # picks the right agent(s) via the router prompt above.
        route_meta["domains"] = routes
        route_meta["agent_labels"] = [AGENT_DISPLAY_NAMES.get(r, r.title()) for r in routes]

        tasks = [run_domain_agent(r) for r in routes]
        results = await asyncio.gather(*tasks)
        combined_insights = "\n\n".join(results)
        # Step 3: Synthesize final answer
        if len(results) == 1:
            return results[0], (route_meta or None)

        context_blocks = "\n".join(b for b in [macro_block, session_block] if b)
        synthesizer_prompt = f"""
        Based on the following expert insights, provide a single, cohesive, and actionable response to the user's query.
        Keep the response professional, concise, and structured. Do not use overly complex formatting if it is to be read aloud.

        User Query: "{query}"

        {context_blocks}

        Expert Insights:
        {combined_insights}
        """
        
        synthesizer = get_synthesizer()
        final_answer = await synthesizer.generate_content_async(synthesizer_prompt)
        
        return final_answer.text, (route_meta or None)
    def process_query(self, query: str, location: str = None, history: list = None) -> str:
        # Deprecated: creating a fresh event loop per call breaks the module-
        # level cached genai.Client, whose async transport binds to whichever
        # loop was live the first time it was used ("Event loop is closed" on
        # every call after the first). Callers should await
        # process_query_async(...) directly on the app's own persistent
        # event loop instead. Kept only for any other sync caller; do not
        # add new uses.
        return asyncio.run(self.process_query_async(query, location=location, history=history))


# ─────────────────────────────────────────────────────────────────────────
# Journal companion — multi-turn Gemini conversation + summarization,
# satisfying the ideathon's "multi-turn interaction with Gemini API for
# journaling" requirement directly (separate from the CFA/multi-agent chat
# above, which is oriented around a single deep answer per turn rather than
# an ongoing reflective conversation). Uses the same Vertex AI client/model
# as everything else in this file — no extra secret/config needed.
# ─────────────────────────────────────────────────────────────────────────
JOURNAL_SYSTEM_INSTRUCTION = """
You are a thoughtful personal journaling companion focused on the user's financial life —
trades, portfolio decisions, money habits, and how they're feeling about them. You are not
a financial advisor giving recommendations here; your job is to help the user reflect, ask
good follow-up questions, and help them notice patterns in their own thinking over time.
Keep replies conversational and concise (a few sentences, not an essay), and end with a
short, genuine follow-up question more often than not, the way a good journaling prompt would.
"""

JOURNAL_SUMMARY_INSTRUCTION = """
Summarize the following journal conversation in 2-4 sentences, in the third person, capturing
the key themes, decisions, or feelings the user expressed — the kind of summary the user would
find useful skimming back over a month of entries. Do not invent details that weren't said.
Return plain text only, no headers or markdown.
"""


def _history_to_contents(history: list[dict]) -> list:
    """Converts a stored [{"role": "user"|"model", "text": ...}, ...] list into
    genai_types.Content objects for a multi-turn call. Unknown roles fall back
    to "user" so a malformed stored entry can't silently get dropped."""
    contents = []
    for turn in history or []:
        role = turn.get("role") if turn.get("role") in ("user", "model") else "user"
        text = turn.get("text", "")
        if text:
            contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=text)]))
    return contents


async def journal_reply_async(history: list[dict]) -> str:
    """Takes the full stored conversation (including the just-appended latest
    user turn) and returns the model's next reply — genuine multi-turn, since
    the whole history is replayed as the conversation each time (Gemini's
    generateContent is stateless server-side; multi-turn is achieved by
    resending prior turns, same pattern Vertex AI's own chat sessions use
    under the hood)."""
    client = get_client()
    contents = _history_to_contents(history)
    if not contents:
        return "I didn't catch anything to respond to — what's on your mind?"
    resp = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=genai_types.GenerateContentConfig(system_instruction=JOURNAL_SYSTEM_INSTRUCTION),
    )
    return (resp.text or "").strip()


async def journal_summary_async(history: list[dict]) -> str:
    """Produces the concise summary described in docs/architecture_blueprint.md
    step 3 ('a background worker requests a concise summary from Gemini'),
    called after a chat turn via FastAPI BackgroundTasks — see
    routers/journals.py."""
    client = get_client()
    contents = _history_to_contents(history)
    if not contents:
        return ""
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=genai_types.GenerateContentConfig(system_instruction=JOURNAL_SUMMARY_INSTRUCTION),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logging.warning(f"Journal summary generation failed, continuing without it: {e}")
        return ""