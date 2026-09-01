import streamlit as st
import requests

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Equity Screener", layout="wide")

st.title("📊 Equity Screener — NSE & BSE")
st.caption(
    "Reads from a Firestore cache refreshed in the background, not fetched live on this page — "
    "the full universe is too large to query on every visit without re-triggering the same rate-limiting "
    "that caused the dashboard ticker outage."
)

with st.expander("What's real vs. what's missing here", expanded=False):
    st.markdown(
        "- **From Yahoo Finance (real, live-cached):** P/E, Forward P/E, P/B, EV/EBITDA, P/S, ROE, "
        "margins, D/E, Current Ratio, Dividend Yield/Payout, Beta, sector/industry, market cap.\n"
        "- **Computed from statements (real, but coverage varies by ticker):** ROCE, Free Cash Flow, "
        "FCF/Net-Income yield, CapEx trend, Asset/Inventory turnover, DSO, Interest Coverage, Revenue CAGR, "
        "Volume growth.\n"
        "- **Not available from free sources — shown as N/A:** exact promoter holding %, promoter pledge %, "
        "true FII/DII trajectory, contingent liabilities, industry TAM, market-share trajectory, and analyst "
        "earnings-revision trends. These need a paid vendor (Screener.in, Trendlyne, Tickertape) or scraping "
        "exchange shareholding filings directly."
    )

@st.cache_data(ttl=60)
def fetch_status():
    try:
        r = requests.get(f"{BACKEND_URL}/api/screener/status", timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

@st.cache_data(ttl=300)
def fetch_sectors():
    try:
        r = requests.get(f"{BACKEND_URL}/api/screener/sectors", timeout=15)
        return r.json().get("sectors", []) if r.status_code == 200 else []
    except Exception:
        return []

@st.cache_data(ttl=60)
def fetch_stocks(search, sector, exchange, sort_by, descending, page, page_size):
    try:
        params = {
            "search": search, "sector": sector, "exchange": exchange,
            "sort_by": sort_by, "descending": descending,
            "page": page, "page_size": page_size,
        }
        r = requests.get(f"{BACKEND_URL}/api/screener/stocks", params=params, timeout=30)
        return r.json() if r.status_code == 200 else {"results": [], "total": 0}
    except Exception:
        return {"results": [], "total": 0}

@st.cache_data(ttl=120)
def fetch_detail(yf_symbol):
    try:
        r = requests.get(f"{BACKEND_URL}/api/screener/stocks/{yf_symbol}", timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

status = fetch_status()
info_col, btn_col = st.columns([5, 1.4])
with info_col:
    if status.get("last_run"):
        cov = f"{status.get('cursor', 0):,} / {status.get('universe_size', 0):,} symbols into current pass"
        st.info(f"Cache last touched: {status['last_run']} UTC · Coverage: {cov} · "
                f"{'✅ at least one full pass completed' if status.get('last_full_pass') else '⏳ first full pass still in progress'}")
    else:
        st.warning("No refresh has run yet — the cache is empty, so the table below has nothing to show. Click 'Populate now' →")
with btn_col:
    if st.button("⚡ Populate now (full)", use_container_width=True, help="Runs a full pass over the entire NSE+BSE universe right now (~5,000 symbols). Uses incremental client-side batching to bypass Cloud Run timeouts."):
        progress_text = "Populating NSE+BSE universe..."
        my_bar = st.progress(0.0, text=progress_text)
        try:
            while True:
                resp = requests.post(f"{BACKEND_URL}/api/screener/refresh", params={"full": False, "batch_size": 10}, timeout=120)
                if resp.status_code != 200:
                    st.error(f"Backend error: {resp.text}")
                    break
                data = resp.json()
                cursor = data.get("cursor", 0)
                total = data.get("universe_size", 1)
                
                pct = min(1.0, max(0.0, cursor / total)) if total > 0 else 0.0
                my_bar.progress(pct, text=f"{progress_text} {cursor:,} / {total:,} symbols")
                
                if data.get("wrapped_full_pass"):
                    break
        except Exception as e:
            st.error(f"Populate interrupted: {e}")
        st.cache_data.clear()
        st.rerun()
st.caption("One-time setup for automatic nightly refresh: raise the backend's request timeout "
           "(`gcloud run services update <service> --region <region> --timeout=3600`) and point a Cloud Scheduler "
           "job at `POST /api/screener/refresh?full=true` with `--time-zone=\"Asia/Kolkata\"` and cron `0 0 * * *` — "
           "then this page stays populated automatically and the button above is only needed for an occasional manual top-up.")

sectors = fetch_sectors()

c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
with c1:
    search = st.text_input("Search by name or symbol", "")
with c2:
    sector = st.selectbox("Sector", [""] + sectors)
with c3:
    exchange = st.selectbox("Exchange", ["", "NSE", "BSE"])
with c4:
    sort_by = st.selectbox(
        "Sort by",
        ["market_cap", "pe_ratio", "roe", "dividend_yield", "revenue_cagr", "debt_to_equity", "volume_growth"],
        format_func=lambda x: x.replace("_", " ").title(),
    )

page_size = 30
if "screener_page" not in st.session_state:
    st.session_state.screener_page = 1

if "filters" not in st.session_state:
    st.session_state.filters = {"search": "", "sector": "", "exchange": "", "sort_by": ""}

current_filters = {"search": search, "sector": sector, "exchange": exchange, "sort_by": sort_by}
if current_filters != st.session_state.filters:
    st.session_state.screener_page = 1
    st.session_state.filters = current_filters

data = fetch_stocks(search, sector, exchange, sort_by, True, st.session_state.screener_page, page_size)
rows = data.get("results", [])
total = data.get("total", 0)

if not rows:
    st.info("No cached stocks match these filters yet.")
else:
    display_rows = []
    for r in rows:
        display_rows.append({
            "Symbol": r.get("symbol"), "Exchange": r.get("exchange"), "Name": r.get("name"),
            "Sector": r.get("sector"), "Mkt Cap": r.get("market_cap"), "Price": r.get("current_price"),
            "P/E": r.get("pe_ratio"), "Fwd P/E": r.get("forward_pe"), "PEG": r.get("peg_ratio"),
            "P/B": r.get("pb_ratio"), "EV/EBITDA": r.get("ev_ebitda"), "P/S": r.get("ps_ratio"),
            "ROE %": r.get("roe"), "ROCE %": r.get("roce"), "NPM %": r.get("net_profit_margin"),
            "OPM %": r.get("opm"), "Rev CAGR %": r.get("revenue_cagr"), "D/E": r.get("debt_to_equity"),
            "Div Yield %": r.get("dividend_yield"), "Vol Growth %": r.get("volume_growth"),
            "QoQ Rev Gr %": r.get("revenue_growth_qoq"), "QoQ NI Gr %": r.get("net_income_growth_qoq"),
            "YoY Rev Gr %": r.get("revenue_growth_yoy"), "YoY NI Gr %": r.get("net_income_growth_yoy"),
        })
    st.dataframe(display_rows, use_container_width=True, hide_index=True)

    p1, p2, p3 = st.columns([1, 2, 1])
    with p1:
        if st.button("◀ Prev", disabled=st.session_state.screener_page <= 1):
            st.session_state.screener_page -= 1
            st.rerun()
    with p2:
        st.markdown(f"<div style='text-align:center;'>Page {st.session_state.screener_page} of {max(1, -(-total // page_size))} ({total:,} matches)</div>", unsafe_allow_html=True)
    with p3:
        if st.button("Next ▶", disabled=st.session_state.screener_page * page_size >= total):
            st.session_state.screener_page += 1
            st.rerun()

st.divider()
st.subheader("Stock detail")
detail_symbol = st.text_input("Enter exact yfinance symbol (e.g. RELIANCE.NS or 500325.BO) for full detail + last 4 quarters")
if detail_symbol:
    detail = fetch_detail(detail_symbol.strip().upper())
    if not detail:
        st.error("Not found in cache yet.")
    else:
        st.json(detail, expanded=False)
        qr = detail.get("quarterly_results", [])
        if qr:
            st.markdown("**Last 4 quarters**")
            st.dataframe(qr, use_container_width=True, hide_index=True)