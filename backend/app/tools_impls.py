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
}

TOOL_IMPLS = {
    "get_market_data": lambda args: get_market_data(args.get("ticker", "")),
    "get_fund_data": lambda args: get_fund_data(args.get("fund_id", "")),
    "get_macro_indicators": lambda args: get_macro_indicators(),
    "get_safe_route": lambda args: get_safe_route(args.get("source", ""), args.get("destination", "")),
    "get_market_movers": lambda args: get_market_movers(),
}