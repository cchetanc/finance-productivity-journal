import html
from urllib.parse import urlparse
import streamlit as st
import requests

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Dividends & Corporate Actions", layout="wide")

st.title("📢 Dividends & Corporate Actions")
st.caption(
    "Headline links sourced via news search, refreshed live on this page — NSE/BSE's own corporate-actions "
    "APIs require browser session cookies and block server-side scraping, so this isn't a structured "
    "ex-date/record-date table, just real, clickable coverage of what's being announced."
)

@st.cache_data(ttl=300)
def fetch(path, limit=20):
    try:
        r = requests.get(f"{BACKEND_URL}{path}", params={"limit": limit}, timeout=20)
        return r.json().get("headlines", []) if r.status_code == 200 else []
    except Exception:
        return []


def render_list(items, empty_msg):
    if not items:
        st.info(empty_msg)
        return
    for h in items:
        raw_link = h.get("link", "") or ""
        parsed = urlparse(raw_link)
        link = raw_link if parsed.scheme in ("http", "https") else None
        title = html.escape(h.get("title") or "")
        source = html.escape(h.get("source") or "")
        pub = html.escape(h.get("published") or "")
        if link:
            st.markdown(f"**[{title}]({link})**")
        else:
            st.markdown(f"**{title}**")
        st.caption(f"{source} · {pub}")
        st.divider()


tab1, tab2 = st.tabs(["Dividends", "Bonus / Splits / Buybacks"])
with tab1:
    render_list(fetch("/api/corporate-actions/dividends"), "No dividend announcements found right now.")
with tab2:
    render_list(fetch("/api/corporate-actions/announcements"), "No bonus/split/buyback announcements found right now.")