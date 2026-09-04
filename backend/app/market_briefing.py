"""
Daily Productivity Assistant — proactive, phase-aware market briefing.

Drives the greeting shown when the CFA chat panel opens. Instead of a
static, repeating one-liner, this looks at the current IST time and picks
one of four phases, then (for the three "market day" phases) grounds the
message in real numbers pulled live via yfinance:

  PRE_MARKET    -> before 9:15 IST on a trading day: a quant-style read on
                   the global/macro factors that typically set NSE/BSE's
                   opening direction (Wall Street close, Asian cues, crude,
                   DXY, US yields, USD/INR), ending in a directional lean.
  MARKET_HOURS  -> 9:15-15:30 IST: "how's it going so far today" using the
                   same live index breadth the dashboard itself uses.
  POST_MARKET   -> after 15:30 IST on a trading day: a same-day wrap-up.
  WEEKEND       -> Sat/Sun: markets are shut, offer to help with something
                   else instead.

Callers (routers/market.py) are responsible for persistence/dedup (i.e.
only calling generate_*() once per phase per day per user) via
database.py's daily-chat helpers — this module only knows how to produce
the text for a given phase, not whether it's already been sent today.
"""
import logging
from datetime import datetime, timezone, timedelta

import yfinance as yf

from .agents import get_client, MODEL_NAME, _strip_code_fences
from .market_data import get_live_indices, compute_market_mood, get_live_news, INDICES

IST = timezone(timedelta(hours=5, minutes=30))

_MARKET_OPEN = (9, 15)
_MARKET_CLOSE = (15, 30)

PRE_MARKET = "PRE_MARKET"
MARKET_HOURS = "MARKET_HOURS"
POST_MARKET = "POST_MARKET"
WEEKEND = "WEEKEND"


def today_ist_str(now_ist: datetime | None = None) -> str:
    """Calendar date key (IST) used to bucket a day's chat in Firestore —
    e.g. '2026-09-03'. Crossing midnight IST naturally starts a fresh
    document, which is what makes "yesterday's messages stay put, today
    starts clean" work without any explicit cleanup job."""
    now_ist = now_ist or datetime.now(IST)
    return now_ist.strftime("%Y-%m-%d")


def determine_market_phase(now_ist: datetime | None = None) -> str:
    now_ist = now_ist or datetime.now(IST)
    if now_ist.weekday() >= 5:  # Sat=5, Sun=6
        return WEEKEND
    open_t = now_ist.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)
    close_t = now_ist.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
    if now_ist < open_t:
        return PRE_MARKET
    if now_ist <= close_t:
        return MARKET_HOURS
    return POST_MARKET


# ─────────────────────────────────────────────────────────────────────────
# Pre-market factor checklist — global cues, commodities/FX, US yields.
# NOTE on GIFT Nifty: there is no reliable free/key-less data feed for the
# actual GIFT City Nifty futures contract, so it is deliberately NOT
# fabricated here. The prompt below is told explicitly that it's unavailable
# so the model reasons from the factors we *do* have real numbers for
# (Wall Street close, Asian cues, crude, DXY, US 10Y, USD/INR) rather than
# inventing a GIFT Nifty premium/discount figure.
# ─────────────────────────────────────────────────────────────────────────
_PREMARKET_TICKERS = {
    "Dow Jones": "^DJI",
    "S&P 500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "Kospi": "^KS11",
    "Brent Crude": "BZ=F",
    "WTI Crude": "CL=F",
    "US Dollar Index (DXY)": "DX-Y.NYB",
    "US 10Y Treasury Yield": "^TNX",
    "USD/INR": "INR=X",
}


def _pct_change(symbol: str) -> dict | None:
    """Same 'last close vs prior close' logic get_live_indices uses,
    factored out so it can be reused for the extra tickers (crude, DXY,
    US 10Y, USD/INR, Asian indices) that aren't in market_data.INDICES."""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty or len(hist) < 2:
            return None
        import math
        price = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        if math.isnan(price) or math.isnan(prev) or prev == 0:
            return None
        change_pct = round(((price - prev) / prev) * 100, 2)
        return {"price": round(price, 2), "change_pct": change_pct}
    except Exception as e:
        logging.warning(f"[premarket] fetch failed for {symbol}: {e}")
        return None


def fetch_premarket_factors() -> dict:
    """Pulls the previous close + %change for every ticker in
    _PREMARKET_TICKERS. Tickers that fail to fetch are simply omitted (not
    zero-filled) so the prompt/model never treats a fetch failure as a real
    flat/zero reading."""
    out = {}
    for name, symbol in _PREMARKET_TICKERS.items():
        data = _pct_change(symbol)
        if data:
            out[name] = data
    return out


def _factors_to_bullets(factors: dict) -> str:
    if not factors:
        return "No live global data could be fetched this run."
    lines = []
    for name, data in factors.items():
        arrow = "▲" if data["change_pct"] >= 0 else "▼"
        lines.append(f"- {name}: {data['price']:,.2f} ({arrow}{abs(data['change_pct']):.2f}%)")
    return "\n".join(lines)


def _call_gemini(prompt: str) -> str:
    client = get_client()
    resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return _strip_code_fences(getattr(resp, "text", "") or "").strip()


def generate_premarket_briefing(now_ist: datetime | None = None) -> str:
    """Expert-quant-style read on today's likely NSE/BSE opening direction,
    grounded in real overnight global data — not the model's memory."""
    now_ist = now_ist or datetime.now(IST)
    factors = fetch_premarket_factors()
    factor_block = _factors_to_bullets(factors)

    prompt = f"""You are an expert equity-markets quant analyst opening a chat with a
trader in Mumbai before the NSE/BSE session begins ({now_ist.strftime('%A, %d %B %Y, %H:%M')} IST).

Here is the live overnight/global data you have (previous close and % change):
{factor_block}

Note: a live GIFT Nifty futures premium/discount feed is NOT available right now — do not
invent a specific GIFT Nifty number. Reason from Wall Street's close, the early Asian
majors (Nikkei/Hang Seng/Kospi), crude oil, the US Dollar Index, US 10-year yields, and
USD/INR instead, the same way those factors are known to feed into Nifty's open.

Write a short (110-150 word) chat message, as if speaking directly to the user, that:
1. Opens with a one-line greeting appropriate for a pre-market message today.
2. Names the 2-3 factors most likely to set today's tone and what direction they point.
3. Gives a clear but appropriately hedged directional lean for the Nifty/Sensex open
   (e.g. "likely a cautious/flat-to-positive start" — never a guaranteed prediction).
4. Ends by inviting the user to ask about a specific stock, sector, or their portfolio.
Do not use markdown headers or bullet points in the reply — write it as natural,
conversational prose, like a message in a chat app. Do not repeat the raw data list
verbatim; synthesize it.
"""
    try:
        text = _call_gemini(prompt)
        if text:
            return text
    except Exception as e:
        logging.warning(f"[premarket] Gemini generation failed, using fallback: {e}")

    # Deterministic fallback if the model call fails for any reason —
    # still useful, just less narrative.
    return (
        "Morning! Here's the pre-open picture — " + factor_block.replace("\n", " | ") +
        ". Want me to dig into a specific stock or sector before the bell?"
    )


def generate_intraday_update(now_ist: datetime | None = None) -> str:
    """"How's the market been going so far today" — for someone connecting
    mid-session, using the same live index breadth as the dashboard. Also
    where the proactive breakout shortlist lives: this is the one message
    generated automatically (once per market-hours phase per day — see the
    module docstring) rather than only on request, so it's the closest
    thing this app has to the quant desk "flagging" names on its own."""
    now_ist = now_ist or datetime.now(IST)
    try:
        indices = get_live_indices()
        mood = compute_market_mood(indices)
        headlines = [h["title"] for h in (get_live_news(limit=5) or [])]
    except Exception as e:
        logging.warning(f"[intraday] live data fetch failed: {e}")
        indices, mood, headlines = [], {"bias": "NEUTRAL", "macro_risk_flags": []}, []

    try:
        from .screener_data import get_breakout_candidates
        breakout = get_breakout_candidates(limit=3)
    except Exception as e:
        logging.warning(f"[intraday] breakout screen failed: {e}")
        breakout = {"candidates": []}

    idx_lines = "\n".join(
        f"- {i['name']}: {i['price']:,.2f} ({'+' if i['positive'] else ''}{i['change_pct']:.2f}%)"
        for i in indices if i.get("price")
    ) or "No live index data available."
    news_lines = "\n".join(f"- {h}" for h in headlines) or "No fresh headlines fetched this run."
    breakout_lines = "\n".join(
        f"- {c['symbol']} ({c.get('name') or ''}): ₹{c['price']}, up {c['five_day_change_pct']}% over 5 "
        f"sessions on {c['volume_growth_pct']}% higher volume"
        for c in breakout.get("candidates") or []
    ) or "None clearing the volume+momentum screen right now."

    prompt = f"""You are a markets desk assistant. The user has just opened the chat
mid-session, at {now_ist.strftime('%H:%M')} IST while the NSE/BSE market is live.

Live index readings right now:
{idx_lines}
Systemic mood: {mood.get('bias')} (score {mood.get('systemic_score')}/100)
Notable moves: {', '.join(mood.get('macro_risk_flags') or []) or 'none notable'}

Recent headlines:
{news_lines}

Breakout screen (5-day momentum confirmed by volume growth — a heuristic shortlist, not a
guaranteed breakout and not a recommendation to act without a closer look):
{breakout_lines}

Write a short (110-150 word) chat message, addressed directly to the user, that:
1. Greets them briefly and states how the market has been trading so far today.
2. Calls out the standout index move(s) and, if a headline clearly explains a move, ties
   it in briefly.
3. If the breakout screen above found real candidates, name them with their actual numbers
   (price, 5-day move %, volume growth %) as a shortlist worth a look — explicitly call this a
   screened heuristic, not a certainty, and invite the user to say the word if they'd like a
   paper trade placed on one of them. If the screen found nothing, don't mention it at all —
   don't manufacture a "nothing to report" line about it.
4. Ends by inviting them to ask about a specific stock, sector, or something off-market
   entirely.
Natural conversational prose, no markdown headers/bullets, no invented numbers beyond
what's given above.
"""
    try:
        text = _call_gemini(prompt)
        if text:
            return text
    except Exception as e:
        logging.warning(f"[intraday] Gemini generation failed, using fallback: {e}")

    return (
        f"Market's live right now — here's where things stand: {idx_lines.replace(chr(10), ' | ')}. "
        "Want a closer look at a specific stock or sector?"
    )


def generate_eod_summary(now_ist: datetime | None = None) -> str:
    """Post-close wrap-up of what happened in the market today."""
    now_ist = now_ist or datetime.now(IST)
    try:
        indices = get_live_indices()
        mood = compute_market_mood(indices)
        headlines = [h["title"] for h in (get_live_news(limit=6) or [])]
    except Exception as e:
        logging.warning(f"[eod] live data fetch failed: {e}")
        indices, mood, headlines = [], {"bias": "NEUTRAL", "macro_risk_flags": []}, []

    idx_lines = "\n".join(
        f"- {i['name']}: {i['price']:,.2f} ({'+' if i['positive'] else ''}{i['change_pct']:.2f}%)"
        for i in indices if i.get("price")
    ) or "No live index data available."
    news_lines = "\n".join(f"- {h}" for h in headlines) or "No fresh headlines fetched this run."

    prompt = f"""You are a markets desk assistant. The NSE/BSE cash session has closed for
the day ({now_ist.strftime('%A, %d %B %Y')}), and the user has just opened the chat.

Closing-ish index readings:
{idx_lines}
Systemic mood for the session: {mood.get('bias')} (score {mood.get('systemic_score')}/100)
Notable moves: {', '.join(mood.get('macro_risk_flags') or []) or 'none notable'}

Headlines from today:
{news_lines}

Write a short (100-140 word) end-of-day chat message, addressed directly to the user, that:
1. Greets them and gives a one-line verdict on how today's session went overall.
2. Notes the standout index/sector move(s) of the day and briefly why, if a headline
   supports it.
3. Ends by asking if they want a deeper dive into a stock/sector, or to plan for
   tomorrow, or something off-market.
Natural conversational prose, no markdown headers/bullets, no invented numbers.
"""
    try:
        text = _call_gemini(prompt)
        if text:
            return text
    except Exception as e:
        logging.warning(f"[eod] Gemini generation failed, using fallback: {e}")

    return (
        f"Markets have closed for the day. Snapshot: {idx_lines.replace(chr(10), ' | ')}. "
        "Want a deeper dive on anything, or should we plan for tomorrow?"
    )


_WEEKEND_MESSAGES = [
    "Happy weekend! Markets are closed, so no trade calls today — want help "
    "planning something instead? I can suggest movies, restaurants nearby, "
    "check your spending, or think through a weekend getaway.",
    "No bells ringing on the exchange today — perfect excuse for a good "
    "movie, a new restaurant, or a short drive somewhere. Want ideas, or "
    "should we get ahead on something for Monday's session instead?",
    "Weekend mode. The tickers can wait — fancy a film recommendation, a "
    "food spot nearby, a look at your recent spending, or a plan for a "
    "quick getaway?",
]


def generate_weekend_message(now_ist: datetime | None = None) -> str:
    now_ist = now_ist or datetime.now(IST)
    idx = now_ist.toordinal() % len(_WEEKEND_MESSAGES)
    return _WEEKEND_MESSAGES[idx]


_PHASE_GENERATORS = {
    PRE_MARKET: generate_premarket_briefing,
    MARKET_HOURS: generate_intraday_update,
    POST_MARKET: generate_eod_summary,
    WEEKEND: generate_weekend_message,
}


def generate_phase_message(phase: str, now_ist: datetime | None = None) -> str:
    gen = _PHASE_GENERATORS.get(phase, generate_weekend_message)
    return gen(now_ist)