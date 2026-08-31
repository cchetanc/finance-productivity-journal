"""
Mutual fund data layer — full AMFI scheme universe, with risk/return metrics
computed from historical NAV, cached in Firestore.

DATA SOURCES AND WHAT THEY DO/DON'T COVER:
- AMFI's own NAVAll.txt (official, free, no key) gives every registered
  scheme's current NAV, scheme name, AMC, and category (equity/debt/hybrid
  etc., as a fund-of-funds-style text heading in the file).
- Historical NAV per scheme (needed to compute CAGR/Alpha/Beta/Sharpe/etc.)
  has no official free bulk API. This uses https://api.mfapi.in — a
  well-known free, no-key, community-run wrapper around AMFI's own
  historical NAV data. It is NOT an official AMFI/SEBI endpoint; treat it
  as best-effort and expect occasional gaps/downtime.
- Everything computed here (CAGR, Alpha, Beta, Sharpe, Sortino, Std Dev,
  R-squared) is real math over that NAV history, benchmarked against Nifty
  50 (^NSEI via yfinance) as a reasonable default for equity-oriented
  schemes.
- Fields with NO free structured source and left as None/"N/A": Expense
  Ratio, Exit Load, AUM, Fund Manager tenure/track record, credit
  quality/average maturity, Modified Duration, YTM, portfolio P/E or P/B,
  sector allocation, equity/debt split, portfolio turnover, benchmark
  deviation, and minimum SIP amount. These live in each AMC's factsheet /
  Scheme Information Document, which has no free structured API — only a
  paid vendor (Value Research, Morningstar, ACE MF) or per-AMC scraping
  provides them.
"""

import datetime
import math
import time
import requests
import yfinance as yf
from google.cloud import firestore

db = firestore.Client()

FUNDS_COLLECTION = "mutual_funds"
META_DOC = db.collection("mutual_funds_meta").document("state")

AMFI_NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_HISTORY_URL = "https://api.mfapi.in/mf/{code}"

RISK_FREE_RATE_ANNUAL = 0.065  # approx. Indian short-term G-sec/repo proxy; update as needed


def fetch_amfi_scheme_list() -> list:
    """Parses AMFI's NAVAll.txt. The file is semicolon-delimited with
    category headers as bare lines (e.g. 'Open Ended Schemes(Equity
    Scheme-Large Cap Fund)') interleaved between data rows — those headers
    become each subsequent scheme's `category` until the next header."""
    try:
        resp = requests.get(AMFI_NAV_ALL_URL, timeout=30)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        schemes = []
        current_category = ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if ";" not in line:
                # Heading line, e.g. an AMC name or a scheme-category label.
                current_category = line
                continue
            parts = line.split(";")
            if len(parts) < 5 or parts[0] == "Scheme Code":
                continue
            code, isin_growth, isin_div, name, nav = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                nav_val = float(nav)
            except Exception:
                continue
            schemes.append({
                "scheme_code": code.strip(),
                "name": name.strip(),
                "category": current_category,
                "nav": nav_val,
                "isin_growth": isin_growth.strip(),
            })
        return schemes
    except Exception as e:
        print(f"[mf] AMFI scheme list fetch failed: {e}")
        return []


def fetch_scheme_history(scheme_code: str) -> list:
    """Full daily NAV history for one scheme, oldest first. Community API
    (see module docstring) — fails soft to []."""
    try:
        resp = requests.get(MFAPI_HISTORY_URL.format(code=scheme_code), timeout=20)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        hist = []
        for row in data:
            try:
                d = datetime.datetime.strptime(row["date"], "%d-%m-%Y").date()
                hist.append((d, float(row["nav"])))
            except Exception:
                continue
        hist.sort(key=lambda x: x[0])
        return hist
    except Exception as e:
        print(f"[mf] history fetch failed for {scheme_code}: {e}")
        return []


_BENCHMARK_CACHE: dict = {}


def _get_benchmark_returns(start: datetime.date, end: datetime.date) -> list:
    """Daily returns of ^NSEI over the window, for Beta/Alpha/R². Cached
    per-process since the same window gets reused across many schemes in a
    batch."""
    key = (start, end)
    if key in _BENCHMARK_CACHE:
        return _BENCHMARK_CACHE[key]
    try:
        hist = yf.Ticker("^NSEI").history(start=start, end=end + datetime.timedelta(days=1))
        closes = hist["Close"].tolist()
        returns = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        _BENCHMARK_CACHE[key] = returns
        return returns
    except Exception as e:
        print(f"[mf] benchmark fetch failed: {e}")
        return []


def _cagr_from_history(hist: list, years_back: float) -> float | None:
    if not hist:
        return None
    end_date, end_nav = hist[-1]
    target_date = end_date - datetime.timedelta(days=int(years_back * 365.25))
    start_point = min(hist, key=lambda x: abs((x[0] - target_date).days))
    if (start_point[0] - target_date).days > 60:
        return None  # not enough history for this horizon
    if start_point[1] <= 0:
        return None
    actual_years = (end_date - start_point[0]).days / 365.25
    if actual_years <= 0:
        return None
    return round((((end_nav / start_point[1]) ** (1 / actual_years)) - 1) * 100, 2)


def compute_risk_metrics(hist: list) -> dict:
    """CAGR (3/5/10yr), Alpha, Beta, Sharpe, Sortino, Std Dev, R-squared —
    computed from daily NAV returns over however much history is available
    (fund-of-funds returns rows: None where history doesn't reach that
    horizon, e.g. a 2-year-old fund has no 5yr/10yr CAGR)."""
    out = {
        "cagr_3y": _cagr_from_history(hist, 3),
        "cagr_5y": _cagr_from_history(hist, 5),
        "cagr_10y": _cagr_from_history(hist, 10),
        "alpha": None, "beta": None, "sharpe_ratio": None, "sortino_ratio": None,
        "standard_deviation": None, "r_squared": None,
    }
    if len(hist) < 30:
        return out

    navs = [n for _, n in hist]
    fund_returns = [(navs[i] / navs[i - 1] - 1) for i in range(1, len(navs)) if navs[i - 1] > 0]
    if len(fund_returns) < 20:
        return out

    mean_r = sum(fund_returns) / len(fund_returns)
    variance = sum((r - mean_r) ** 2 for r in fund_returns) / (len(fund_returns) - 1)
    std_dev = math.sqrt(variance)
    out["standard_deviation"] = round(std_dev * math.sqrt(252) * 100, 2)  # annualized, %

    downside = [r for r in fund_returns if r < 0]
    if downside:
        downside_dev = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
        daily_rf = RISK_FREE_RATE_ANNUAL / 252
        out["sortino_ratio"] = round(((mean_r - daily_rf) / downside_dev) * math.sqrt(252), 2) if downside_dev else None

    daily_rf = RISK_FREE_RATE_ANNUAL / 252
    if std_dev:
        out["sharpe_ratio"] = round(((mean_r - daily_rf) / std_dev) * math.sqrt(252), 2)

    bench_returns = _get_benchmark_returns(hist[0][0], hist[-1][0])
    n = min(len(fund_returns), len(bench_returns))
    if n >= 20:
        fr = fund_returns[-n:]
        br = bench_returns[-n:]
        mean_b = sum(br) / n
        cov = sum((fr[i] - mean_r) * (br[i] - mean_b) for i in range(n)) / (n - 1)
        var_b = sum((b - mean_b) ** 2 for b in br) / (n - 1)
        if var_b:
            beta = cov / var_b
            out["beta"] = round(beta, 2)
            annual_fund_return = mean_r * 252
            annual_bench_return = mean_b * 252
            out["alpha"] = round((annual_fund_return - (RISK_FREE_RATE_ANNUAL + beta * (annual_bench_return - RISK_FREE_RATE_ANNUAL))) * 100, 2)
            # R-squared via correlation²
            std_f = math.sqrt(sum((x - mean_r) ** 2 for x in fr) / (n - 1))
            std_b = math.sqrt(var_b)
            if std_f and std_b:
                corr = cov / (std_f * std_b)
                out["r_squared"] = round((corr ** 2) * 100, 2)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# FIRESTORE CACHE — read side
# ─────────────────────────────────────────────────────────────────────────────
def get_funds_page(search: str = "", category: str = "",
                    sort_by: str = "cagr_5y", descending: bool = True,
                    page: int = 1, page_size: int = 50) -> dict:
    query = db.collection(FUNDS_COLLECTION)
    if category:
        query = query.where("category", "==", category)
    docs = list(query.limit(4000).stream())
    rows = [d.to_dict() for d in docs]

    if search:
        s = search.strip().lower()
        rows = [r for r in rows if s in (r.get("name") or "").lower()]

    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or 0), reverse=descending)

    total = len(rows)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size, "results": rows[start:start + page_size]}


def get_fund_detail(scheme_code: str) -> dict | None:
    doc = db.collection(FUNDS_COLLECTION).document(scheme_code).get()
    return doc.to_dict() if doc.exists else None


def get_categories() -> list:
    docs = db.collection(FUNDS_COLLECTION).select(["category"]).limit(4000).stream()
    return sorted({d.to_dict().get("category") for d in docs if d.to_dict().get("category")})


def get_refresh_status() -> dict:
    doc = META_DOC.get()
    return doc.to_dict() if doc.exists else {"cursor": 0, "universe_size": 0, "last_run": None, "last_full_pass": None}


# ─────────────────────────────────────────────────────────────────────────────
# FIRESTORE CACHE — write side (the refresh job)
# ─────────────────────────────────────────────────────────────────────────────
_FIRESTORE_BATCH_LIMIT = 400  # Firestore caps a single batch at 500 writes; stay under it


def _build_scheme_doc(scheme: dict) -> dict:
    hist = fetch_scheme_history(scheme["scheme_code"])
    metrics = compute_risk_metrics(hist)
    return {
        **scheme,
        **metrics,
        "last_updated": datetime.datetime.utcnow().isoformat(),
        # Fields with no free source — see module docstring.
        "expense_ratio": None, "exit_load": None, "aum": None,
        "fund_manager_tenure": None, "fund_manager_track_record": None,
        "portfolio_pe": None, "portfolio_pb": None,
        "equity_debt_split": None, "sector_concentration": None,
        "credit_quality": None, "average_maturity": None,
        "modified_duration": None, "ytm": None,
        "benchmark_deviation": None, "portfolio_turnover": None,
        "min_sip_amount": None, "information_ratio": None,
        "treynor_ratio": (round((metrics.get("alpha") or 0) / metrics["beta"], 2)
                          if metrics.get("beta") else None),
        "upside_capture": None, "downside_capture": None,
        "rolling_returns": None, "tracking_error": None,
    }


def _fetch_and_commit(schemes: list, request_delay_sec: float) -> int:
    """Same incremental-commit pattern as screener_data._fetch_and_commit —
    commits each Firestore-safe sub-batch as soon as it fills, so a run
    that dies partway still keeps whatever it already computed."""
    processed = 0
    batch = db.batch()
    pending = 0
    for scheme in schemes:
        try:
            data = _build_scheme_doc(scheme)
            doc_ref = db.collection(FUNDS_COLLECTION).document(scheme["scheme_code"])
            batch.set(doc_ref, data, merge=True)
            pending += 1
            processed += 1
        except Exception as e:
            print(f"[mf] failed {scheme.get('scheme_code')}: {e}")

        if pending >= _FIRESTORE_BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            pending = 0

        time.sleep(request_delay_sec)

    if pending:
        batch.commit()
    return processed


def run_refresh_batch(batch_size: int = 80, request_delay_sec: float = 0.3) -> dict:
    """Incremental mode — one bounded slice starting from the saved cursor.
    Useful as a manual top-up (the 'Populate now' button). For routine
    daily coverage of the whole universe, use run_full_refresh() instead."""
    meta = get_refresh_status()
    universe = fetch_amfi_scheme_list()
    universe_size = len(universe)

    if universe_size == 0:
        print("[mf] AMFI scheme list fetch returned 0 — aborting batch.")
        return {"processed": 0, "universe_size": 0, "error": "AMFI fetch failed"}

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


def run_full_refresh(request_delay_sec: float = 0.3) -> dict:
    """Processes every AMFI scheme (~2,500) in one call — schedule this
    nightly (midnight IST) via Cloud Scheduler for a fully-populated Mutual
    Funds page with no manual triggering needed.

    DEPLOYMENT NOTE: each scheme needs a full NAV-history fetch (not just a
    single lightweight call), so a full pass over ~2,500 schemes will take
    considerably longer than the equity screener's full refresh — budget at
    least an hour, likely more, and raise the Cloud Run service timeout to
    its max accordingly:

        gcloud run services update <service-name> \\
            --region <region> --timeout=3600

    Even at the 3600s ceiling this may not finish one scheme universe in a
    single request; progress commits incrementally (see _fetch_and_commit),
    so a nightly run that gets cut off still leaves most schemes freshly
    updated rather than none. If a single request consistently isn't
    enough, consider running this from a Cloud Run Job instead of an HTTP
    service, which has no request-timeout ceiling.
    """
    universe = fetch_amfi_scheme_list()
    universe_size = len(universe)

    if universe_size == 0:
        print("[mf] AMFI scheme list fetch returned 0 — aborting full refresh.")
        return {"processed": 0, "universe_size": 0, "error": "AMFI fetch failed"}

    processed = _fetch_and_commit(universe, request_delay_sec)

    now = datetime.datetime.utcnow().isoformat()
    META_DOC.set({
        "cursor": 0,
        "universe_size": universe_size,
        "last_run": now,
        "last_full_pass": now,
    }, merge=True)

    return {"processed": processed, "universe_size": universe_size, "mode": "full"}