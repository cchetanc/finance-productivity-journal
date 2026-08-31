"""
Equity screener data layer — builds the combined NSE+BSE stock universe and
fetches per-stock fundamentals, caching everything in Firestore.

WHY CACHED, NOT LIVE:
Fetching ~5,000 tickers synchronously per page load is not viable — it's the
same class of problem that caused the index-ticker outage (Yahoo Finance
rate-limits/blocks bursty request patterns, especially from cloud IPs). So
this module is designed to run as an incremental background refresh:

    POST /api/screener/refresh   (see routers/screener.py)

processes one bounded batch (default 150 symbols) starting from wherever
the last run left off, and persists the cursor in Firestore. Point a Cloud
Scheduler job at that endpoint every few minutes and the full ~5,000-symbol
universe gets covered incrementally, without ever holding a long-lived
request open or bursting requests. The frontend screener page only ever
reads the Firestore cache — it never calls Yahoo Finance directly.

DATA AVAILABILITY — READ BEFORE EXTENDING:
yfinance's `.info` dict (backed by Yahoo Finance, free/no-key) reliably
covers a real subset of what a full equity-research screener wants: P/E,
Forward P/E, P/B, EV/EBITDA, P/S, ROE, margins, D/E, current ratio,
dividend yield/payout, beta, sector/industry, market cap. Several
statement-level metrics (ROCE, CapEx trend, FCF, asset/inventory turnover,
DSO, interest coverage) are computed here from the raw financial statements
yfinance also exposes — but these are only as complete/consistent as
Yahoo's own statement data for a given ticker, which varies a lot for
small/micro-cap NSE/BSE names.

The following fields the user asked for have **no free, structured data
source** and are intentionally left as None/"N/A" rather than faked:
promoter holding %, promoter pledge %, real FII/DII holding trajectory
(vs. the closest free proxy, `heldPercentInsiders`), contingent
liabilities, industry TAM, market-share trajectory, and analyst
earnings-revision trends. Getting real values for these requires a paid
vendor (Screener.in, Trendlyne, Tickertape, Capitaline) or scraping NSE/BSE
shareholding-pattern filings directly.
"""

import time
import datetime
import requests
import yfinance as yf
from google.cloud import firestore

db = firestore.Client()

STOCKS_COLLECTION = "screener_stocks"
META_DOC = db.collection("screener_meta").document("state")

# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE SOURCES
# ─────────────────────────────────────────────────────────────────────────────
# Public reference-data files. Neither is an official documented API — NSE
# and BSE both occasionally change these paths or add bot-detection, so
# fetch_* below fail soft (log + return []) rather than crashing the batch.
#
# NOTE (fixed): this URL previously used the singular '/content/equity/'
# path, which 404s — NSE's archive path is plural, '/content/equities/'.
# Confirmed live and working as of this fix.
NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_SCRIP_LIST_URL  = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# api.bseindia.com rejects requests that don't look like they came from a
# browser tab actually on bseindia.com — without Origin/Referer it returns
# an HTML block page instead of JSON (surfaces as a
# "Expecting value: line 1 column 1" JSON-decode error). These two headers
# on top of _BROWSER_HEADERS are what a real browser sends automatically.
_BSE_HEADERS = {
    **_BROWSER_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}

# Hand-curated thematic overrides — Yahoo's sector/industry taxonomy has no
# "EV" category, so anything finer-grained than sector/industry (which IS
# freely available) needs manual tagging. Extend this dict as needed; it's
# additive on top of the sector/industry Yahoo already gives every stock.
THEME_TAGS: dict[str, list[str]] = {
    "TATAMOTORS.NS": ["EV", "Auto"],
    "M&M.NS": ["EV", "Auto"],
    "OLAELEC.NS": ["EV"],
    "EXIDEIND.NS": ["EV", "Battery"],
    "AMARAJABAT.NS": ["EV", "Battery"],
    "ADANIGREEN.NS": ["Renewable Energy"],
    "TATAPOWER.NS": ["Renewable Energy", "Energy"],
    "NTPC.NS": ["Energy", "Power"],
    "POWERGRID.NS": ["Energy", "Power"],
    "ONGC.NS": ["Energy", "Oil & Gas"],
    "RELIANCE.NS": ["Energy", "Oil & Gas", "Telecom", "Retail"],
}


# nsearchives.nseindia.com mirrors the same archive path as a fallback —
# NSE has changed the primary host before (www/archives/nsearchives), so
# trying a second host beats hard-failing the whole universe build.
NSE_EQUITY_LIST_URL_FALLBACK = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def _parse_nse_csv(text: str) -> list:
    lines = text.splitlines()
    header = [c.strip() for c in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < len(header):
            continue
        rec = dict(zip(header, [p.strip() for p in parts]))
        series = rec.get("SERIES", "")
        if series not in ("EQ", "BE"):
            continue
        symbol = rec.get("SYMBOL", "")
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "yf_symbol": f"{symbol}.NS",
            "name": rec.get("NAME OF COMPANY", symbol),
            "isin": rec.get(" ISIN NUMBER", rec.get("ISIN NUMBER", "")).strip(),
            "exchange": "NSE",
        })
    return rows


def fetch_nse_equity_list() -> list:
    """Full NSE-listed equity universe (~2,000 symbols): symbol, name, ISIN,
    series. Series 'EQ'/'BE' are the tradeable equity series; others (debt,
    etc.) are skipped. Tries the primary archive host, then a known mirror,
    before giving up — logs response status + a body snippet on failure so
    a future path change is diagnosable from logs alone, not guesswork."""
    for url in (NSE_EQUITY_LIST_URL, NSE_EQUITY_LIST_URL_FALLBACK):
        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
            resp.raise_for_status()
            rows = _parse_nse_csv(resp.text)
            if rows:
                return rows
            print(f"[screener] NSE list fetch from {url} returned 0 parsable rows — response started with: {resp.text[:200]!r}")
        except Exception as e:
            print(f"[screener] NSE list fetch failed for {url}: {e}")
    return []


def fetch_bse_equity_list() -> list:
    """Full BSE-listed active equity universe. BSE's own API uses numeric
    scrip codes — yfinance addresses BSE tickers as '<scripcode>.BO'."""
    try:
        params = {
            "Group": "", "Scripcode": "", "industry": "",
            "segment": "Equity", "status": "Active",
        }
        resp = requests.get(BSE_SCRIP_LIST_URL, headers=_BSE_HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            print(f"[screener] BSE list fetch got non-JSON response (status {resp.status_code}) — body started with: {resp.text[:200]!r}")
            return []
        rows = []
        for rec in data if isinstance(data, list) else data.get("Table", []):
            scrip_code = str(rec.get("SCRIP_CD") or rec.get("SC_CODE") or "").strip()
            if not scrip_code:
                continue
            rows.append({
                "symbol": rec.get("SC_NAME") or rec.get("Scrip_Name") or scrip_code,
                "yf_symbol": f"{scrip_code}.BO",
                "name": rec.get("SC_NAME") or rec.get("Scrip_Name") or scrip_code,
                "isin": (rec.get("ISIN_NUMBER") or rec.get("ISIN") or "").strip(),
                "exchange": "BSE",
            })
        return rows
    except Exception as e:
        print(f"[screener] BSE list fetch failed: {e}")
        return []


def build_universe() -> list:
    """Combined NSE+BSE universe, deduplicated by ISIN (NSE preferred when a
    stock is dual-listed, since its yfinance data tends to be more complete).
    Falls back to whichever single list is reachable if the other fails, so
    a BSE outage doesn't zero out the whole universe."""
    nse = fetch_nse_equity_list()
    bse = fetch_bse_equity_list()

    seen_isin = {r["isin"] for r in nse if r["isin"]}
    combined = list(nse)
    for r in bse:
        if r["isin"] and r["isin"] in seen_isin:
            continue
        combined.append(r)
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# PER-STOCK FUNDAMENTALS
# ─────────────────────────────────────────────────────────────────────────────
def _safe(fn, default=None):
    try:
        v = fn()
        return v if v not in (None, "", "None") else default
    except Exception:
        return default


def _cagr(first: float, last: float, years: float) -> float | None:
    if not first or not last or first <= 0 or years <= 0:
        return None
    try:
        return round((((last / first) ** (1 / years)) - 1) * 100, 2)
    except Exception:
        return None


def fetch_fundamentals(yf_symbol: str) -> dict:
    """Pulls everything yfinance/statement math can give for one symbol.
    Every field is wrapped so one missing/odd value never breaks the row —
    it just comes back None (rendered as 'N/A' by the frontend)."""
    t = yf.Ticker(yf_symbol)
    info = _safe(lambda: t.info, {}) or {}

    out = {
        "yf_symbol": yf_symbol,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "themes": THEME_TAGS.get(yf_symbol, []),
        "market_cap": info.get("marketCap"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),

        # Valuation
        "pe_ratio": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
        "pb_ratio": info.get("priceToBook"),
        "ev_ebitda": info.get("enterpriseToEbitda"),
        "ps_ratio": info.get("priceToSalesTrailing12Months"),

        # Profitability
        "roe": _pct(info.get("returnOnEquity")),
        "net_profit_margin": _pct(info.get("profitMargins")),
        "opm": _pct(info.get("operatingMargins")),
        "eps_growth": _pct(info.get("earningsGrowth")),
        "revenue_growth_yoy": _pct(info.get("revenueGrowth")),

        # Leverage / liquidity
        "debt_to_equity": _de_ratio(info.get("debtToEquity")),
        "current_ratio": info.get("currentRatio"),

        # Payout
        "dividend_yield": _pct(info.get("dividendYield")),
        "dividend_payout_ratio": _pct(info.get("payoutRatio")),

        # Free-data proxies for otherwise-unavailable ownership metrics —
        # NOT the exact NSE-disclosed promoter holding %.
        "insider_holding_proxy": _pct(info.get("heldPercentInsiders")),
        "institutional_holding_proxy": _pct(info.get("heldPercentInstitutions")),

        "beta": info.get("beta"),

        # Fields with no free structured source — see module docstring.
        "roce": None,
        "promoter_holding_pct": None,
        "promoter_pledge_pct": None,
        "fii_dii_trajectory": None,
        "contingent_liabilities": None,
        "industry_tam": None,
        "market_share_trajectory": None,
        "earnings_revision_trend": None,

        "last_updated": datetime.datetime.utcnow().isoformat(),
    }

    _enrich_from_statements(t, out)
    _enrich_volume_growth(t, out)
    return out


def _pct(v):
    """yfinance returns ratios like 0.184 for 18.4% — normalize to a
    percentage number, but only for values that look like ratios (some
    fields already come back as whole percentages depending on symbol)."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    return round(v * 100, 2) if -5 < v < 5 else round(v, 2)


def _de_ratio(v):
    """yfinance's debtToEquity is typically already a percentage
    (e.g. 45.2 meaning 0.452x) — normalize to a plain ratio (x)."""
    if v is None:
        return None
    try:
        return round(float(v) / 100, 2)
    except Exception:
        return None


def _enrich_from_statements(t: "yf.Ticker", out: dict) -> None:
    """Computes ROCE, FCF, FCF/NI yield, CapEx trend, asset/inventory
    turnover, DSO, interest coverage, and multi-year revenue/EPS CAGR from
    the raw annual financial statements. Every lookup is independently
    guarded — Yahoo's statement coverage is inconsistent, especially for
    small/micro-cap names, so partial data is expected and fine."""
    try:
        financials = _safe(lambda: t.financials, None)      # annual income statement
        balance    = _safe(lambda: t.balance_sheet, None)    # annual balance sheet
        cashflow   = _safe(lambda: t.cashflow, None)         # annual cash flow

        def row(df, *names):
            if df is None:
                return None
            for n in names:
                if n in df.index:
                    return df.loc[n]
            return None

        ebit      = row(financials, "EBIT", "Ebit")
        interest  = row(financials, "Interest Expense")
        revenue   = row(financials, "Total Revenue")
        net_inc   = row(financials, "Net Income")
        assets    = row(balance, "Total Assets")
        cur_liab  = row(balance, "Current Liabilities", "Total Current Liabilities")
        inventory = row(balance, "Inventory")
        cogs      = row(financials, "Cost Of Revenue", "Reconciled Cost Of Revenue")
        receivable= row(balance, "Accounts Receivable", "Receivables")
        op_cf     = row(cashflow, "Operating Cash Flow", "Total Cash From Operating Activities")
        capex     = row(cashflow, "Capital Expenditure")

        # ROCE = EBIT / (Total Assets - Current Liabilities), most recent year
        if ebit is not None and assets is not None and cur_liab is not None and len(ebit) and len(assets) and len(cur_liab):
            capital_employed = float(assets.iloc[0]) - float(cur_liab.iloc[0])
            if capital_employed > 0:
                out["roce"] = round(float(ebit.iloc[0]) / capital_employed * 100, 2)

        # Interest coverage = EBIT / Interest Expense
        if ebit is not None and interest is not None and len(ebit) and len(interest) and float(interest.iloc[0]) != 0:
            out["interest_coverage"] = round(float(ebit.iloc[0]) / abs(float(interest.iloc[0])), 2)
        else:
            out["interest_coverage"] = None

        # Free cash flow = Operating CF - |CapEx|, most recent year
        if op_cf is not None and capex is not None and len(op_cf) and len(capex):
            fcf = float(op_cf.iloc[0]) - abs(float(capex.iloc[0]))
            out["free_cash_flow"] = round(fcf, 0)
            if net_inc is not None and len(net_inc) and float(net_inc.iloc[0]) != 0:
                out["fcf_to_net_income_yield"] = round(fcf / float(net_inc.iloc[0]) * 100, 2)
            else:
                out["fcf_to_net_income_yield"] = None
        else:
            out["free_cash_flow"] = None
            out["fcf_to_net_income_yield"] = None

        # CapEx trend — most recent year vs. prior year, % change
        if capex is not None and len(capex) >= 2 and float(capex.iloc[1]) != 0:
            out["capex_trend_yoy"] = round((float(capex.iloc[0]) - float(capex.iloc[1])) / abs(float(capex.iloc[1])) * 100, 2)
        else:
            out["capex_trend_yoy"] = None

        # Asset turnover = Revenue / Total Assets
        if revenue is not None and assets is not None and len(revenue) and len(assets) and float(assets.iloc[0]) != 0:
            out["asset_turnover"] = round(float(revenue.iloc[0]) / float(assets.iloc[0]), 2)
        else:
            out["asset_turnover"] = None

        # Inventory turnover = COGS / Inventory
        if cogs is not None and inventory is not None and len(cogs) and len(inventory) and float(inventory.iloc[0]) != 0:
            out["inventory_turnover"] = round(float(cogs.iloc[0]) / float(inventory.iloc[0]), 2)
        else:
            out["inventory_turnover"] = None

        # DSO = (Receivables / Revenue) * 365
        if receivable is not None and revenue is not None and len(receivable) and len(revenue) and float(revenue.iloc[0]) != 0:
            out["days_sales_outstanding"] = round(float(receivable.iloc[0]) / float(revenue.iloc[0]) * 365, 1)
        else:
            out["days_sales_outstanding"] = None

        # Multi-year revenue/EPS CAGR — from however many annual columns
        # Yahoo actually returns (commonly 4, sometimes fewer).
        if revenue is not None and len(revenue) >= 2:
            years = len(revenue) - 1
            out["revenue_cagr"] = _cagr(float(revenue.iloc[-1]), float(revenue.iloc[0]), years)
        else:
            out["revenue_cagr"] = None

        eps = _safe(lambda: t.get_earnings_history(), None)
        out["eps_growth_multi_year"] = None  # left None; needs consistent multi-year EPS series not reliably exposed for most NSE/BSE names

    except Exception as e:
        print(f"[screener] statement enrichment failed for {out.get('yf_symbol')}: {e}")


def _enrich_volume_growth(t: "yf.Ticker", out: dict) -> None:
    """Volume growth = avg daily volume (last 20 sessions) vs. the prior 20
    sessions — a real, computable signal from price history, no statement
    data needed. Also captures the latest single-session price move and a
    5-session move from the same history pull (no extra API call) — this
    pairing (volume spike + notable price move) is what the CFA assistant's
    market-movers tool below uses as a rough 'breakout candidate' filter."""
    try:
        hist = t.history(period="3mo")
        if hist is None or hist.empty or "Volume" not in hist or "Close" not in hist:
            out["volume_growth"] = None
            out["day_change_pct"] = None
            out["five_day_change_pct"] = None
            return

        closes = hist["Close"].dropna()
        if len(closes) >= 2:
            out["day_change_pct"] = round((closes.iloc[-1] / closes.iloc[-2] - 1) * 100, 2)
        else:
            out["day_change_pct"] = None
        if len(closes) >= 6:
            out["five_day_change_pct"] = round((closes.iloc[-1] / closes.iloc[-6] - 1) * 100, 2)
        else:
            out["five_day_change_pct"] = None

        vol = hist["Volume"].dropna()
        if len(vol) < 40:
            out["volume_growth"] = None
            return
        recent = vol.iloc[-20:].mean()
        prior  = vol.iloc[-40:-20].mean()
        out["volume_growth"] = round((recent - prior) / prior * 100, 2) if prior else None
    except Exception:
        out["volume_growth"] = None
        out["day_change_pct"] = None
        out["five_day_change_pct"] = None


def get_quarterly_results(yf_symbol: str, n: int = 4) -> list:
    """Last n quarters of revenue/net income/EPS, most recent first."""
    try:
        t = yf.Ticker(yf_symbol)
        qf = _safe(lambda: t.quarterly_financials, None)
        if qf is None or qf.empty:
            return []
        cols = list(qf.columns)[:n]
        results = []
        for c in cols:
            rev = qf.loc["Total Revenue", c] if "Total Revenue" in qf.index else None
            ni  = qf.loc["Net Income", c] if "Net Income" in qf.index else None
            results.append({
                "period": c.strftime("%b %Y") if hasattr(c, "strftime") else str(c),
                "revenue": round(float(rev), 0) if rev is not None else None,
                "net_income": round(float(ni), 0) if ni is not None else None,
            })
        return results
    except Exception as e:
        print(f"[screener] quarterly results failed for {yf_symbol}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# FIRESTORE CACHE — read side (used by the API routes)
# ─────────────────────────────────────────────────────────────────────────────
def get_stocks_page(search: str = "", sector: str = "", exchange: str = "",
                     sort_by: str = "market_cap", descending: bool = True,
                     page: int = 1, page_size: int = 50) -> dict:
    """Reads from the Firestore cache only — never touches yfinance. Filters
    beyond exchange/sector are applied in Python after a bounded Firestore
    read, since Firestore can't do free-text search or combine arbitrary
    filters with a sort without composite indexes we don't want to force
    the user to hand-create for a first pass."""
    query = db.collection(STOCKS_COLLECTION)
    if exchange:
        query = query.where("exchange", "==", exchange.upper())
    if sector:
        query = query.where("sector", "==", sector)

    docs = list(query.limit(4000).stream())
    rows = [d.to_dict() for d in docs]

    if search:
        s = search.strip().lower()
        rows = [r for r in rows if s in (r.get("name") or "").lower() or s in (r.get("symbol") or "").lower()]

    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or 0), reverse=descending)

    total = len(rows)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": rows[start:start + page_size],
    }


def get_stock_detail(yf_symbol: str) -> dict | None:
    doc = db.collection(STOCKS_COLLECTION).document(yf_symbol).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["quarterly_results"] = get_quarterly_results(yf_symbol, n=4)
    return data


def get_sectors() -> list:
    docs = db.collection(STOCKS_COLLECTION).select(["sector"]).limit(4000).stream()
    return sorted({d.to_dict().get("sector") for d in docs if d.to_dict().get("sector")})


def get_market_movers(limit: int = 8) -> dict:
    """Rough 'what's worth watching right now' shortlist for the CFA chat's
    market-movers tool — reads the screener cache only (no live yfinance
    calls, so this stays fast and doesn't add to Yahoo Finance load), and
    surfaces:
      - top volume-growth names (unusual trading activity — the classic
        precursor screeners flag as an early breakout signal), and
      - the largest single-session price movers among those,
    Both computed straight from cached history, not invented. Returns
    empty lists (with a note) rather than an error if the screener cache
    hasn't been populated yet — the caller (the equity agent) is expected
    to say so plainly rather than pretend it has data it doesn't."""
    docs = list(db.collection(STOCKS_COLLECTION)
                .where("volume_growth", ">", 0)
                .limit(1000).stream())
    rows = [d.to_dict() for d in docs]

    if not rows:
        return {"note": "Screener cache is empty or not yet covering enough symbols — no movers to report from cached data yet.", "high_volume": [], "biggest_movers": []}

    def _slim(r):
        return {
            "symbol": r.get("symbol"), "exchange": r.get("exchange"), "name": r.get("name"),
            "sector": r.get("sector"), "price": r.get("current_price"),
            "volume_growth_pct": r.get("volume_growth"),
            "day_change_pct": r.get("day_change_pct"),
            "five_day_change_pct": r.get("five_day_change_pct"),
        }

    by_volume = sorted(rows, key=lambda r: r.get("volume_growth") or 0, reverse=True)[:limit]

    movers_pool = [r for r in rows if r.get("day_change_pct") is not None]
    by_move = sorted(movers_pool, key=lambda r: abs(r.get("day_change_pct") or 0), reverse=True)[:limit]

    return {
        "high_volume": [_slim(r) for r in by_volume],
        "biggest_movers": [_slim(r) for r in by_move],
        "note": "Sourced from the equity screener's Firestore cache, not a live tick-by-tick scan — freshness depends on when the cache was last refreshed.",
    }


def get_refresh_status() -> dict:
    try:
        doc = META_DOC.get()
        return doc.to_dict() if doc.exists else {"cursor": 0, "universe_size": 0, "last_run": None, "last_full_pass": None}
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


# ─────────────────────────────────────────────────────────────────────────────
# FIRESTORE CACHE — write side (the refresh job)
# ─────────────────────────────────────────────────────────────────────────────
_FIRESTORE_BATCH_LIMIT = 400  # Firestore caps a single batch at 500 writes; stay under it


def _fetch_and_commit(refs: list, request_delay_sec: float) -> int:
    """Fetches fundamentals for a list of universe entries and commits them
    to Firestore in Firestore-safe sub-batches, committing each sub-batch as
    soon as it's full rather than holding everything in memory until the
    very end — so a run that dies partway (timeout, Yahoo block, container
    restart) still keeps whatever it already fetched instead of losing the
    whole pass."""
    processed = 0
    batch = db.batch()
    pending = 0
    for ref in refs:
        try:
            data = fetch_fundamentals(ref["yf_symbol"])
            data["symbol"] = ref["symbol"]
            data["exchange"] = ref["exchange"]
            data["isin"] = ref.get("isin")
            doc_ref = db.collection(STOCKS_COLLECTION).document(ref["yf_symbol"])
            batch.set(doc_ref, data, merge=True)
            pending += 1
            processed += 1
        except Exception as e:
            print(f"[screener] failed {ref.get('yf_symbol')}: {e}")

        if pending >= _FIRESTORE_BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            pending = 0

        time.sleep(request_delay_sec)

    if pending:
        batch.commit()
    return processed


def run_refresh_batch(batch_size: int = 150, request_delay_sec: float = 0.35) -> dict:
    """Incremental mode — processes one bounded slice of the universe,
    starting from the saved cursor, and advances the cursor for next time.
    Useful as a manual top-up (the 'Populate now' button) or a
    frequently-scheduled small-batch job. For routine daily coverage of the
    WHOLE universe, use run_full_refresh() instead — see its docstring."""
    meta = get_refresh_status()
    universe = build_universe()
    universe_size = len(universe)

    if universe_size == 0:
        print("[screener] universe build returned 0 symbols — NSE/BSE list fetch likely blocked; aborting batch.")
        return {"processed": 0, "universe_size": 0, "error": "universe fetch failed"}

    cursor = meta.get("cursor", 0) % universe_size
    slice_ = universe[cursor: cursor + batch_size]
    wrapped = False
    if len(slice_) < batch_size:
        wrapped = True
        slice_ += universe[0: batch_size - len(slice_)]

    processed = _fetch_and_commit(slice_, request_delay_sec)

    new_cursor = (cursor + batch_size) % universe_size
    now = datetime.datetime.utcnow().isoformat()
    meta_update = {"cursor": new_cursor, "universe_size": universe_size, "last_run": now}
    if wrapped or new_cursor < cursor:
        meta_update["last_full_pass"] = now
    META_DOC.set(meta_update, merge=True)

    return {"processed": processed, "universe_size": universe_size, "cursor": new_cursor, "wrapped_full_pass": wrapped}


def run_full_refresh(request_delay_sec: float = 0.35) -> dict:
    """Processes the ENTIRE NSE+BSE universe in one call — this is what
    should run once nightly (e.g. midnight IST) via Cloud Scheduler, so the
    screener always shows a fully-populated table without anyone needing to
    trigger anything by hand.

    IMPORTANT DEPLOYMENT NOTE: at ~5,000 symbols and a paced
    request_delay_sec, one full pass takes roughly
    (universe_size * request_delay_sec) seconds — around 30 minutes at the
    default pacing — plus per-symbol fetch time on top of that. Cloud Run's
    DEFAULT request timeout is only 300 seconds, which is nowhere near
    enough. Before scheduling this, raise the backend service's timeout:

        gcloud run services update <service-name> \\
            --region <region> --timeout=3600

    Progress is still committed incrementally (see _fetch_and_commit), so
    even if the request does get cut off, whatever was processed before
    that point is already saved — a partial nightly run still leaves you
    with mostly-fresh data rather than nothing.
    """
    universe = build_universe()
    universe_size = len(universe)

    if universe_size == 0:
        print("[screener] universe build returned 0 symbols — NSE/BSE list fetch likely blocked; aborting full refresh.")
        return {"processed": 0, "universe_size": 0, "error": "universe fetch failed"}

    processed = _fetch_and_commit(universe, request_delay_sec)

    now = datetime.datetime.utcnow().isoformat()
    META_DOC.set({
        "cursor": 0,
        "universe_size": universe_size,
        "last_run": now,
        "last_full_pass": now,
    }, merge=True)

    return {"processed": processed, "universe_size": universe_size, "mode": "full"}