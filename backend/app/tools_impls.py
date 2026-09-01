"""
Synchronous tool implementations used by the Groq function-calling loop in
agents.py. These mirror get_market_data / get_fund_data / get_macro_indicators
from finance_mcp_server.py, but are plain functions (no MCP transport, no
Firestore dependency) so they're cheap to call inline while handling a chat
turn.
"""
import logging
import os
import requests
import yfinance as yf

log = logging.getLogger(__name__)

# Bounds how many cached docs a single chat-triggered screen will scan —
# matches the cap already used elsewhere (screener_data/mf_data read paths)
# so a filter tool call can't turn into an unbounded Firestore read.
_SCREENER_SCAN_LIMIT = 20000


def query_equity_screener(
    pe_min: float = None, pe_max: float = None,
    revenue_qoq_min: float = None,
    roe_min: float = None,
    roce_min: float = None,
    de_max: float = None,
    current_ratio_min: float = None,
    dividend_yield_min: float = None,
    fundamental_score_min: float = None,
    sector: str = None,
    exchange: str = None,
    market_cap_min: float = None,
    sort_by: str = "market_cap",
    limit: int = 10
) -> dict:
    """Agent tool to query the fundamental stock screener cache based on
    numeric criteria. `sort_by` can be any numeric field on the cached
    rows — e.g. 'fundamental_score' to answer 'best fundamentals' style
    questions without a metric being named."""
    from .screener_data import db, STOCKS_COLLECTION
    try:
        docs = db.collection(STOCKS_COLLECTION).limit(_SCREENER_SCAN_LIMIT).stream()
        results = []
        for d in docs:
            data = d.to_dict()
            if sector and sector.lower() not in (data.get("sector") or "").lower(): continue
            if exchange and (data.get("exchange") or "").upper() != exchange.upper(): continue
            if pe_min is not None:
                pe = data.get("pe_ratio")
                if pe is None or pe < pe_min: continue
            if pe_max is not None:
                pe = data.get("pe_ratio")
                if pe is None or pe > pe_max: continue
            if revenue_qoq_min is not None:
                rev = data.get("revenue_growth_qoq")
                if rev is None or rev < revenue_qoq_min: continue
            if roe_min is not None:
                roe = data.get("roe")
                if roe is None or roe < roe_min: continue
            if roce_min is not None:
                roce = data.get("roce")
                if roce is None or roce < roce_min: continue
            if de_max is not None:
                de = data.get("debt_to_equity")
                if de is None or de > de_max: continue
            if current_ratio_min is not None:
                cr = data.get("current_ratio")
                if cr is None or cr < current_ratio_min: continue
            if dividend_yield_min is not None:
                dy = data.get("dividend_yield")
                if dy is None or dy < dividend_yield_min: continue
            if fundamental_score_min is not None:
                fs = data.get("fundamental_score")
                if fs is None or fs < fundamental_score_min: continue
            if market_cap_min is not None:
                mc = data.get("market_cap")
                if mc is None or mc < market_cap_min: continue

            results.append(data)

        results.sort(key=lambda x: (x.get(sort_by) is None, x.get(sort_by) or 0), reverse=True)
        return {"matches": results[:limit], "total_matches": len(results)}
    except Exception as e:
        log.error("Failed to query equity screener: %s", e)
        return {"error": str(e)}

def query_mutual_fund_screener(
    category: str = None,
    cagr_1y_min: float = None,
    cagr_3y_min: float = None,
    cagr_5y_min: float = None,
    sharpe_min: float = None,
    alpha_min: float = None,
    std_dev_max: float = None,
    quality_score_min: float = None,
    sort_by: str = "cagr_3y",
    limit: int = 10
) -> dict:
    """Agent tool to query the mutual fund screener cache based on
    performance/risk criteria. `sort_by` can be any numeric field on the
    cached rows — e.g. 'quality_score' to answer 'best mutual funds'
    style questions without a metric being named."""
    from .mf_data import db, FUNDS_COLLECTION
    try:
        docs = db.collection(FUNDS_COLLECTION).limit(_SCREENER_SCAN_LIMIT).stream()
        results = []
        for d in docs:
            data = d.to_dict()
            if category and category.lower() not in (data.get("category") or "").lower(): continue
            if cagr_1y_min is not None:
                cagr = data.get("cagr_1y")
                if cagr is None or cagr < cagr_1y_min: continue
            if cagr_3y_min is not None:
                cagr = data.get("cagr_3y")
                if cagr is None or cagr < cagr_3y_min: continue
            if cagr_5y_min is not None:
                cagr = data.get("cagr_5y")
                if cagr is None or cagr < cagr_5y_min: continue
            if sharpe_min is not None:
                sharpe = data.get("sharpe_ratio")
                if sharpe is None or sharpe < sharpe_min: continue
            if alpha_min is not None:
                alpha = data.get("alpha")
                if alpha is None or alpha < alpha_min: continue
            if std_dev_max is not None:
                sd = data.get("standard_deviation")
                if sd is None or sd > std_dev_max: continue
            if quality_score_min is not None:
                qs = data.get("quality_score")
                if qs is None or qs < quality_score_min: continue

            results.append(data)

        results.sort(key=lambda x: (x.get(sort_by) is None, x.get(sort_by) or -999), reverse=True)
        return {"matches": results[:limit], "total_matches": len(results)}
    except Exception as e:
        log.error("Failed to query MF screener: %s", e)
        return {"error": str(e)}


# Indian small/mid-cap and newly-listed IPO names are almost always what
# users ask about here, but the LLM will often pass the bare company name
# ("Tempsens Instruments India Ltd") instead of a resolvable Yahoo Finance
# ticker. Try the raw symbol first, then the common NSE/BSE suffixes before
# giving up.
_INDIA_SUFFIXES = ["", ".NS", ".BO"]


def _has_price(info: dict) -> bool:
    return bool(info) and bool(
        info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    )


def _try_symbol(symbol: str) -> dict | None:
    try:
        info = yf.Ticker(symbol).info
        # yfinance doesn't raise on an unknown ticker — it just returns a
        # near-empty info dict. Treat "no price data at all" as a miss and
        # let the caller move on to the next candidate instead of returning junk.
        if _has_price(info):
            info = dict(info)
            info["symbol"] = info.get("symbol", symbol)
            return info
    except Exception as e:
        log.warning("yfinance lookup failed for %s: %s", symbol, e)
    return None


def _fetch_ticker_info(raw_ticker: str) -> dict | None:
    candidate = raw_ticker.strip().upper().replace(" ", "")

    # 1. Try it as a literal symbol, with common Indian-exchange suffixes.
    for suffix in _INDIA_SUFFIXES:
        symbol = candidate if candidate.endswith((".NS", ".BO")) else f"{candidate}{suffix}"
        info = _try_symbol(symbol)
        if info:
            return info

    # 2. The model is often handed a company name rather than a real ticker
    # (e.g. "Tempsens Instruments India Ltd"). Use Yahoo Finance's own
    # search/autocomplete to resolve the name to a symbol, then retry.
    try:
        results = yf.Search(raw_ticker.strip(), max_results=5, news_count=0, raise_errors=False).quotes
    except Exception as e:
        log.warning("yfinance company-name search failed for %r: %s", raw_ticker, e)
        results = []

    for quote in results or []:
        symbol = quote.get("symbol")
        if not symbol:
            continue
        info = _try_symbol(symbol)
        if info:
            return info

    return None


def get_market_data(ticker: str) -> dict:
    if not ticker or not ticker.strip():
        return {"error": "No ticker provided."}

    info = _fetch_ticker_info(ticker)
    if info is None:
        # Don't just error out silently — tell the model plainly that live
        # data isn't available for this name so it can say so to the user
        # and fall back to general/qualitative knowledge instead of
        # inventing numbers.
        return {
            "error": (
                f"Could not resolve '{ticker}' to a live ticker on Yahoo Finance "
                f"(tried plain symbol, .NS, and .BO suffixes). It may be too "
                f"newly listed to have data yet, or the name/symbol may need "
                f"to be more precise."
            )
        }

    return {
        "symbol": info.get("symbol", ticker),
        "shortName": info.get("shortName"),
        "currentPrice": info.get("currentPrice") or info.get("regularMarketPrice"),
        "previousClose": info.get("previousClose"),
        "marketCap": info.get("marketCap"),
        "trailingPE": info.get("trailingPE"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }


def get_fund_data(fund_id: str) -> dict:
    if not fund_id or not fund_id.strip():
        return {"error": "No fund ticker provided."}

    info = _fetch_ticker_info(fund_id)
    if info is None:
        return {"error": f"Could not resolve '{fund_id}' to a live fund/ETF ticker on Yahoo Finance."}

    return {
        "symbol": info.get("symbol", fund_id),
        "shortName": info.get("shortName"),
        "navPrice": info.get("navPrice") or info.get("currentPrice"),
        "previousClose": info.get("previousClose"),
        "ytdReturn": info.get("ytdReturn"),
        "expenseRatio": info.get("annualReportExpenseRatio"),
        "category": info.get("category"),
    }


def get_market_movers() -> dict:
    """Trader-facing 'what's worth watching right now' shortlist — high
    trading-volume growth and the biggest single-session price moves,
    sourced from the equity screener's Firestore cache (see
    screener_data.get_market_movers). Deliberately takes no arguments: this
    is meant for exactly the 'what's moving today' / 'anything to watch'
    style question where the user hasn't named a stock."""
    from .screener_data import get_market_movers as _get_market_movers
    try:
        return _get_market_movers(limit=8)
    except Exception as e:
        log.warning("get_market_movers failed: %s", e)
        return {"error": f"Could not read the screener cache: {e}", "high_volume": [], "biggest_movers": []}


_MACRO_TICKERS = {"10Y_Treasury": "^TNX", "13W_Treasury": "^IRX", "S&P500": "^GSPC", "Gold": "GC=F"}


def get_macro_indicators() -> dict:
    data = {}
    for key, symbol in _MACRO_TICKERS.items():
        try:
            info = yf.Ticker(symbol).info
            data[key] = info.get("previousClose", info.get("regularMarketPreviousClose"))
        except Exception as e:
            log.warning("yfinance macro lookup failed for %s: %s", symbol, e)
            data[key] = None
    return {"macro_indicators": data}


# PathSense — separately deployed safe-route platform (see
# github.com/cchetanc/pathsense-safe-route-platform). Its /api/route
# endpoint runs its own weather/disaster/traffic risk-scoring pipeline
# across candidate routes and returns a Gemini-written explanation — this
# app doesn't recompute any of that, it just calls out to it and passes the
# result through.
PATHSENSE_API_BASE = os.environ.get(
    "PATHSENSE_API_BASE", "https://pathsense-api-779524765901.us-central1.run.app"
)


def _risk_band(score) -> str | None:
    """Mirrors PathSense's own frontend thresholds (riskColor() in
    frontend/index.html) so the band label we narrate always matches the
    color legend PathSense itself shows — Low < 25, Moderate < 50, High <
    75, else Severe."""
    if score is None:
        return None
    try:
        score = float(score)
    except (TypeError, ValueError):
        return None
    if score < 25:
        return "Low"
    if score < 50:
        return "Moderate"
    if score < 75:
        return "High"
    return "Severe"


def get_safe_route(source: str, destination: str) -> dict:
    if not source or not source.strip() or not destination or not destination.strip():
        return {"error": "Both a source and a destination are required to plan a route."}
    source, destination = source.strip(), destination.strip()

    try:
        resp = requests.post(
            f"{PATHSENSE_API_BASE}/api/route",
            json={"source": source, "destination": destination},
            # PathSense's own pipeline fans out to several Gemini calls per
            # candidate route (weather/disaster/traffic agents + synthesis),
            # so this is genuinely slower than the yfinance lookups above —
            # give it real headroom rather than timing out mid-plan.
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"error": "PathSense didn't respond in time — its risk-scoring pipeline can be slow on a cold start. Try again in a moment."}
    except Exception as e:
        log.warning("PathSense /api/route call failed: %s", e)
        return {"error": f"Couldn't reach the route-planning service: {e}"}

    # Response shape (confirmed against PathSense's own frontend, since the
    # README's field names didn't quite match reality): top-level
    # "explanation", and "routes": [{route_index, recommended, distance_km,
    # duration_minutes, overall_risk_score, steps: [{instruction, ...}]}].
    routes = data.get("routes") or []
    recommended = next((r for r in routes if r.get("recommended")), routes[0] if routes else {})
    steps = recommended.get("steps") or []

    result = {
        "source": source,
        "destination": destination,
        "explanation": data.get("explanation"),
        "distance_km": recommended.get("distance_km"),
        "duration_minutes": recommended.get("duration_minutes"),
        "risk_score": recommended.get("overall_risk_score"),
        "risk_band": _risk_band(recommended.get("overall_risk_score")),
        # Instruction text only — cap so a long highway route doesn't blow
        # up the tool-response context with 60+ maneuver steps.
        "steps": [s.get("instruction") for s in steps[:20] if s.get("instruction")],
    }

    # Best-departure timing shares the same {source, destination} contract —
    # a natural companion to "what's the safest route", so fetch it in the
    # same tool call rather than making the model ask a second time. This is
    # genuinely optional context: if it fails, the route answer above still
    # stands on its own, so failures here are swallowed rather than turning
    # the whole tool call into an error.
    try:
        dep_resp = requests.post(
            f"{PATHSENSE_API_BASE}/api/route/best-departure",
            json={"source": source, "destination": destination},
            timeout=20,
        )
        if dep_resp.ok:
            dep_data = dep_resp.json()
            result["best_departure_time"] = dep_data.get("best_departure_time")
            result["departure_advice"] = dep_data.get("explanation")
    except Exception as e:
        log.info("PathSense best-departure lookup skipped (non-fatal): %s", e)

    return result




# Groq/OpenAI-format function-calling schemas, keyed by tool name so agents
# can declare which ones they're allowed to use via SimpleAgent.tools.
TOOL_SCHEMAS = {
    "get_market_data": {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": (
                "Retrieve live equity/commodity price and fundamentals for a ticker symbol "
                "or company name. For Indian equities, pass the company name or NSE symbol "
                "as-is — resolution to the correct .NS/.BO ticker is handled automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol or company name, e.g. AAPL, GC=F, 'Tempsens Instruments'"}
                },
                "required": ["ticker"],
            },
        },
    },
    "get_fund_data": {
        "type": "function",
        "function": {
            "name": "get_fund_data",
            "description": "Retrieve mutual fund or ETF NAV, category, and expense ratio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fund_id": {"type": "string", "description": "Fund ticker, e.g. SPY, VOO"}
                },
                "required": ["fund_id"],
            },
        },
    },
    "get_macro_indicators": {
        "type": "function",
        "function": {
            "name": "get_macro_indicators",
            "description": "Retrieve current treasury yields, S&P 500 level, and gold price.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "get_market_movers": {
        "type": "function",
        "function": {
            "name": "get_market_movers",
            "description": (
                "Returns a short, real (non-invented) list of NSE/BSE stocks currently showing "
                "unusually high trading-volume growth and/or notable recent price moves — i.e. "
                "candidates worth watching, sourced from the cached equity screener. Use this when "
                "the user asks about 'market movers', 'what's moving today', 'anything to watch', "
                "or similar, WITHOUT naming a specific stock. Takes no arguments. If the cache is "
                "empty, says so plainly — don't fall back to guessing stock names."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "query_equity_screener": {
        "type": "function",
        "function": {
            "name": "query_equity_screener",
            "description": (
                "Query the fundamental stock screener cache for stocks matching numeric criteria "
                "(PE, ROE, ROCE, D/E, current ratio, dividend yield, QoQ revenue growth, market cap, "
                "sector/exchange). Use 'sort_by': 'fundamental_score' and no other filters when the "
                "user wants the 'best'/'strongest' stocks without naming a specific metric — "
                "fundamental_score is a transparent 0-100 blend of ROE, ROCE, margin, revenue CAGR, "
                "D/E and current ratio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pe_min": {"type": "number", "description": "Minimum PE ratio"},
                    "pe_max": {"type": "number", "description": "Maximum PE ratio"},
                    "revenue_qoq_min": {"type": "number", "description": "Minimum Quarter-over-Quarter revenue growth % (e.g. 20 for 20%)"},
                    "roe_min": {"type": "number", "description": "Minimum Return on Equity %"},
                    "roce_min": {"type": "number", "description": "Minimum Return on Capital Employed %"},
                    "de_max": {"type": "number", "description": "Maximum Debt/Equity ratio (x)"},
                    "current_ratio_min": {"type": "number", "description": "Minimum current ratio"},
                    "dividend_yield_min": {"type": "number", "description": "Minimum dividend yield %"},
                    "fundamental_score_min": {"type": "number", "description": "Minimum composite fundamental quality score (0-100)"},
                    "sector": {"type": "string", "description": "Sector name to filter by"},
                    "exchange": {"type": "string", "description": "'NSE' or 'BSE' to filter by exchange"},
                    "market_cap_min": {"type": "number", "description": "Minimum market cap"},
                    "sort_by": {"type": "string", "description": "Field to sort results by, descending (default 'market_cap'; use 'fundamental_score' for 'best stocks' style questions)"},
                    "limit": {"type": "number", "description": "Max number of results to return (default 10)"}
                }
            }
        }
    },
    "query_mutual_fund_screener": {
        "type": "function",
        "function": {
            "name": "query_mutual_fund_screener",
            "description": (
                "Query the mutual fund screener cache for funds matching performance/risk criteria "
                "(1/3/5-year CAGR, Sharpe, Alpha, volatility, category). Use 'sort_by': 'quality_score' "
                "and no other filters when the user wants the 'best' funds without naming a specific "
                "metric — quality_score is a transparent 0-100 blend of CAGR, Sharpe, Alpha and "
                "(inverted) volatility."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Category name to filter by (e.g. 'Large Cap Fund')"},
                    "cagr_1y_min": {"type": "number", "description": "Minimum 1-year CAGR %"},
                    "cagr_3y_min": {"type": "number", "description": "Minimum 3-year CAGR %"},
                    "cagr_5y_min": {"type": "number", "description": "Minimum 5-year CAGR %"},
                    "sharpe_min": {"type": "number", "description": "Minimum Sharpe ratio"},
                    "alpha_min": {"type": "number", "description": "Minimum Alpha"},
                    "std_dev_max": {"type": "number", "description": "Maximum annualized standard deviation % (volatility ceiling)"},
                    "quality_score_min": {"type": "number", "description": "Minimum composite quality score (0-100)"},
                    "sort_by": {"type": "string", "description": "Field to sort results by, descending (default 'cagr_3y'; use 'quality_score' for 'best funds' style questions)"},
                    "limit": {"type": "number", "description": "Max number of results to return (default 10)"}
                }
            }
        }
    },
    "get_hotel_availability": {
        "type": "function",
        "function": {
            "name": "get_hotel_availability",
            "description": "Check for live hotel room availability, lowest prices, and room counts using Amadeus given a geolocation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number", "description": "Latitude of the target location"},
                    "longitude": {"type": "number", "description": "Longitude of the target location"},
                    "radius": {"type": "number", "description": "Search radius in km (default 5)"},
                    "check_in_date": {"type": "string", "description": "Check in date YYYY-MM-DD"},
                    "check_out_date": {"type": "string", "description": "Check out date YYYY-MM-DD"},
                    "adults": {"type": "number", "description": "Number of adults (default 1)"}
                },
                "required": ["latitude", "longitude", "check_in_date", "check_out_date"]
            }
        }
    }
}

def _get_hotel_availability_wrapper(args):
    from .amadeus_client import get_hotel_availability as amadeus_avail
    try:
        return amadeus_avail(
            lat=args.get("latitude"),
            lon=args.get("longitude"),
            radius=args.get("radius", 5),
            check_in=args.get("check_in_date"),
            check_out=args.get("check_out_date"),
            adults=args.get("adults", 1)
        )
    except Exception as e:
        log.error("Failed to fetch hotel availability: %s", e)
        return {"error": str(e)}

TOOL_IMPLS = {
    "get_market_data": lambda args: get_market_data(args.get("ticker", "")),
    "get_fund_data": lambda args: get_fund_data(args.get("fund_id", "")),
    "get_macro_indicators": lambda args: get_macro_indicators(),
    "get_safe_route": lambda args: get_safe_route(args.get("source", ""), args.get("destination", "")),
    "get_market_movers": lambda args: get_market_movers(),
    "query_equity_screener": lambda args: query_equity_screener(
        pe_min=args.get("pe_min"), pe_max=args.get("pe_max"),
        revenue_qoq_min=args.get("revenue_qoq_min"), roe_min=args.get("roe_min"),
        roce_min=args.get("roce_min"), de_max=args.get("de_max"),
        current_ratio_min=args.get("current_ratio_min"),
        dividend_yield_min=args.get("dividend_yield_min"),
        fundamental_score_min=args.get("fundamental_score_min"),
        sector=args.get("sector"), exchange=args.get("exchange"),
        market_cap_min=args.get("market_cap_min"),
        sort_by=args.get("sort_by", "market_cap"), limit=args.get("limit", 10),
    ),
    "query_mutual_fund_screener": lambda args: query_mutual_fund_screener(
        category=args.get("category"), cagr_1y_min=args.get("cagr_1y_min"),
        cagr_3y_min=args.get("cagr_3y_min"), cagr_5y_min=args.get("cagr_5y_min"),
        sharpe_min=args.get("sharpe_min"), alpha_min=args.get("alpha_min"),
        std_dev_max=args.get("std_dev_max"), quality_score_min=args.get("quality_score_min"),
        sort_by=args.get("sort_by", "cagr_3y"), limit=args.get("limit", 10),
    ),
    "get_hotel_availability": _get_hotel_availability_wrapper,
}