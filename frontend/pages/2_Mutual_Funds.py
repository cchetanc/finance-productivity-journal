import streamlit as st
import requests

from auth_helper import login_widget
from ui_helpers import hide_streamlit_chrome, render_page_nav, render_typeahead_search

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Mutual Funds", layout="wide", initial_sidebar_state="collapsed")
hide_streamlit_chrome()

if not login_widget(BACKEND_URL):
    st.stop()

render_page_nav()

st.title("💰 Mutual Funds — All AMFI Schemes")
st.caption("Reads from a Firestore cache built from AMFI scheme data + NAV-history-derived risk metrics — not fetched live per page load.")

with st.expander("What's real vs. what's missing here", expanded=False):
    st.markdown(
        "- **From AMFI + computed NAV math (real):** current NAV, category, CAGR (3/5/10yr), Alpha, Beta, "
        "Sharpe, Sortino, Standard Deviation, R-squared — all computed from actual historical NAV vs. Nifty 50 "
        "as benchmark.\n"
        "- **Not available from free sources — shown as N/A:** Expense Ratio, Exit Load, AUM, Fund Manager "
        "tenure/track record, portfolio P/E or P/B, sector allocation, equity/debt split, credit quality, "
        "average maturity, modified duration, YTM, minimum SIP amount, benchmark deviation, portfolio turnover. "
        "These live in each AMC's factsheet, which has no free structured API — a paid vendor (Value Research, "
        "Morningstar, ACE MF) is the realistic source if you need them filled in."
    )

@st.cache_data(ttl=60)
def fetch_status():
    try:
        r = requests.get(f"{BACKEND_URL}/api/mutual-funds/status", timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

@st.cache_data(ttl=300)
def fetch_categories():
    try:
        r = requests.get(f"{BACKEND_URL}/api/mutual-funds/categories", timeout=15)
        return r.json().get("categories", []) if r.status_code == 200 else []
    except Exception:
        return []

@st.cache_data(ttl=60)
def fetch_funds(search, category, sort_by, descending, page, page_size):
    try:
        params = {
            "search": search, "category": category,
            "sort_by": sort_by, "descending": descending,
            "page": page, "page_size": page_size,
        }
        r = requests.get(f"{BACKEND_URL}/api/mutual-funds", params=params, timeout=30)
        return r.json() if r.status_code == 200 else {"results": [], "total": 0}
    except Exception:
        return {"results": [], "total": 0}

status = fetch_status()
info_col, btn_col = st.columns([5, 1.4])
with info_col:
    if status.get("last_run"):
        cov = f"{status.get('cursor', 0):,} / {status.get('universe_size', 0):,} schemes into current pass"
        st.info(f"Cache last touched: {status['last_run']} UTC · Coverage: {cov} · "
                f"{'✅ at least one full pass completed' if status.get('last_full_pass') else '⏳ first full pass still in progress'}")
    else:
        st.warning("No refresh has run yet — the cache is empty, so the table below has nothing to show. Click 'Populate now' →")
with btn_col:
    if st.button("⚡ Populate now (full)", use_container_width=True, help="Runs a full pass over every AMFI scheme right now (~2,500 schemes). Uses incremental client-side batching to bypass Cloud Run timeouts."):
        progress_text = "Populating AMFI universe..."
        my_bar = st.progress(0.0, text=progress_text)
        try:
            while True:
                resp = requests.post(f"{BACKEND_URL}/api/mutual-funds/refresh", params={"full": False, "batch_size": 5}, timeout=120)
                if resp.status_code != 200:
                    st.error(f"Backend error: {resp.text}")
                    break
                data = resp.json()
                cursor = data.get("cursor", 0)
                total = data.get("universe_size", 1)
                
                pct = min(1.0, max(0.0, cursor / total)) if total > 0 else 0.0
                my_bar.progress(pct, text=f"{progress_text} {cursor:,} / {total:,} schemes")
                
                if data.get("wrapped_full_pass"):
                    break
        except Exception as e:
            st.error(f"Populate interrupted: {e}")
        st.cache_data.clear()
        st.rerun()
st.caption("One-time setup for automatic nightly refresh: raise the backend's request timeout "
           "(`gcloud run services update <service> --region <region> --timeout=3600`) and point a Cloud Scheduler "
           "job at `POST /api/mutual-funds/refresh?full=true` with `--time-zone=\"Asia/Kolkata\"` and cron `0 0 * * *` — "
           "then this page stays populated automatically. If one pass genuinely can't finish inside 3600s, consider moving "
           "this refresh to a Cloud Run Job instead of an HTTP endpoint, which has no request-timeout ceiling.")

categories = fetch_categories()

c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    # Fragment-scoped type-ahead: suggestions appear as fast as the Strategy dropdown
    # on the Trade Terminal, and the table below only refetches once you pick one.
    search = render_typeahead_search(
        BACKEND_URL, "mf", key_prefix="mf",
        label="Search by scheme name", placeholder="e.g. Parag Parikh Flexi Cap",
    )
with c2:
    category = st.selectbox("Category", [""] + categories)
with c3:
    sort_by = st.selectbox("Sort by", ["cagr_5y", "cagr_3y", "cagr_10y", "sharpe_ratio", "alpha", "beta", "standard_deviation"],
                            format_func=lambda x: x.replace("_", " ").title())

page_size = 30
if "mf_page" not in st.session_state:
    st.session_state.mf_page = 1

data = fetch_funds(search, category, sort_by, True, st.session_state.mf_page, page_size)
rows = data.get("results", [])
total = data.get("total", 0)

if not rows:
    st.info("No cached schemes match these filters yet.")
else:
    display_rows = [{
        "Scheme": r.get("name"), "Category": r.get("category"), "NAV": r.get("nav"),
        "3Y CAGR %": r.get("cagr_3y"), "5Y CAGR %": r.get("cagr_5y"), "10Y CAGR %": r.get("cagr_10y"),
        "Alpha": r.get("alpha"), "Beta": r.get("beta"), "Sharpe": r.get("sharpe_ratio"),
        "Sortino": r.get("sortino_ratio"), "Std Dev %": r.get("standard_deviation"), "R²": r.get("r_squared"),
    } for r in rows]
    st.dataframe(display_rows, use_container_width=True, hide_index=True)

    p1, p2, p3 = st.columns([1, 2, 1])
    with p1:
        if st.button("◀ Prev", disabled=st.session_state.mf_page <= 1):
            st.session_state.mf_page -= 1
            st.rerun()
    with p2:
        st.markdown(f"<div style='text-align:center;'>Page {st.session_state.mf_page} of {max(1, -(-total // page_size))} ({total:,} matches)</div>", unsafe_allow_html=True)
    with p3:
        if st.button("Next ▶", disabled=st.session_state.mf_page * page_size >= total):
            st.session_state.mf_page += 1
            st.rerun()