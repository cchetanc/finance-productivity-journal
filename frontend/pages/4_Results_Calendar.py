import html
from urllib.parse import urlparse
import streamlit as st
import requests

from auth_helper import login_widget
from ui_helpers import hide_streamlit_chrome, render_page_nav

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Results Calendar", layout="wide", initial_sidebar_state="collapsed")
hide_streamlit_chrome()

if not login_widget(BACKEND_URL):
    st.stop()

render_page_nav()

st.title("📅 Quarterly Results Calendar")
st.caption(
    "Headline links sourced via news search, refreshed live on this page — not a structured exchange-fed "
    "results calendar (NSE/BSE's own announcement APIs block server-side scraping), but real, clickable "
    "coverage of upcoming and just-announced results."
)

@st.cache_data(ttl=300)
def fetch(limit=30):
    try:
        r = requests.get(f"{BACKEND_URL}/api/corporate-actions/results-calendar", params={"limit": limit}, timeout=20)
        return r.json().get("headlines", []) if r.status_code == 200 else []
    except Exception:
        return []

items = fetch()
if not items:
    st.info("No results-announcement headlines found right now.")
else:
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