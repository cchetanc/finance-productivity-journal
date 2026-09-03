"""
Shared chrome for every sub-page (Equity Screener, Mutual Funds, Dividends,
Results Calendar, Trade Terminal, Admin):

  - hide_streamlit_chrome(): same "no sidebar / no header / no footer, dark
    theme" CSS app.py already applies to the home page, kept in one place
    so every page looks and behaves the same way.
  - render_page_nav(): a single "◀ Back" + "🏠 Home" + "Signed in as ... ·
    Sign out" bar. This is the ONLY place a Sign out button is rendered
    outside of app.py's own account bar — pages must not add their own, or
    the app ends up with two Sign out buttons live at once (see
    trade_terminal_widget.py, which used to render its own).
  - predictive_search(): a lightweight type-ahead. Streamlit already
    reruns on every keystroke, so this just takes whatever's currently in
    a text_input, asks the backend for a handful of matches, and renders
    them as click-to-fill suggestion rows right under the box.
"""
import html as _html

import requests
import streamlit as st
import streamlit.components.v1 as components

from auth_helper import get_role, logout


def hide_streamlit_chrome():
    st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    background-color: #15120e !important;
    color: #e8ddc7 !important;
}
header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { padding: 18px 26px 26px 26px !important; max-width: 100% !important; }
[data-testid="stButton"] > button {
    background: transparent !important;
    border: 1px solid #463b28 !important;
    color: #a99872 !important;
    font-weight: 600 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 10px !important;
    letter-spacing: 0.5px !important;
    padding: 3px 9px !important;
    border-radius: 5px !important;
    line-height: 1.4 !important;
    min-height: 0 !important;
    height: auto !important;
}
[data-testid="stButton"] > button:hover { border-color: #d3a94a !important; color: #d3a94a !important; }
</style>
""", unsafe_allow_html=True)


def render_page_nav():
    """Back / Home / account bar. Call once, right after the login_widget()
    gate, on every page except the home page itself."""
    nav_l, nav_r = st.columns([1, 8])
    with nav_l:
        # Real browser-history back (not a fixed "previous page" — whatever
        # page actually brought the user here, Home or Admin or a
        # bookmark), rendered as a components.html button styled to match
        # the rest of the app's outlined buttons.
        components.html("""
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  button {
    background: transparent; border: 1px solid #463b28; color: #a99872;
    font-weight: 600; font-family: 'JetBrains Mono', monospace; font-size: 10px;
    letter-spacing: 0.5px; padding: 4px 10px; border-radius: 5px; cursor: pointer;
  }
  button:hover { border-color: #d3a94a; color: #d3a94a; }
</style>
<button onclick="window.parent.history.back()">&#9664; Back</button>
""", height=32)
    with nav_r:
        pass
    
    _is_admin_user = get_role() == "admin"
    if _is_admin_user:
        home_col, admin_col, _empty = st.columns([1, 1, 6])
        with home_col:
            st.page_link("app.py", label="🏠 Home")
        with admin_col:
            st.page_link("pages/6_admin.py", label="🛡️ Admin", help="Control what each user's home page shows")
    else:
        st.page_link("app.py", label="🏠 Home")

    acct_l, acct_r = st.columns([8, 1.4])
    with acct_l:
        role_tag = " · Admin" if _is_admin_user else ""
        st.caption(f"Signed in as {st.session_state.get('fb_email', '')}{role_tag}")
    with acct_r:
        if st.button("Sign out", key="page_signout"):
            logout()
            st.rerun()
    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)


def predictive_search(backend_url: str, kind: str, query: str, key_prefix: str, limit: int = 6):
    """Renders up to `limit` type-ahead suggestion rows for whatever's
    currently typed in a search box, and returns the picked (symbol, name)
    tuple if the user clicked one this run, else None. Fires from the very
    first character (no debounce — Streamlit only reruns on an actual
    keystroke, so there's nothing to throttle).

    kind: "equity" -> /api/screener/stocks, "mf" -> /api/mutual-funds
    """
    q = (query or "").strip()
    if not q:
        return None

    @st.cache_data(ttl=30)
    def _search(q: str, kind: str):
        try:
            if kind == "equity":
                r = requests.get(f"{backend_url}/api/screener/stocks",
                                  params={"search": q, "page_size": limit}, timeout=10)
                if r.status_code == 200:
                    return [
                        {"label": s.get("symbol", ""), "sub": f'{s.get("name","")} · {s.get("exchange","")}'}
                        for s in r.json().get("results", [])
                    ]
            elif kind == "mf":
                r = requests.get(f"{backend_url}/api/mutual-funds",
                                  params={"search": q, "page_size": limit}, timeout=10)
                if r.status_code == 200:
                    return [
                        {"label": s.get("name", ""), "sub": s.get("category", "")}
                        for s in r.json().get("results", [])
                    ]
        except Exception:
            pass
        return []

    results = _search(q, kind)
    if not results:
        return None

    st.markdown("""
<style>
div[class*="st-key-predictive_box"] {
    background: #211b13; border: 1px solid #332b1f; border-radius: 8px;
    padding: 6px 10px; margin: -6px 0 10px 0;
}
div[class*="st-key-predictive_box"] button {
    width: 100%;
    text-align: left !important;
    background: transparent !important;
    border: none !important;
    color: #e8ddc7 !important;
    font-size: 13px !important;
    padding: 5px 4px !important;
    justify-content: flex-start !important;
    box-shadow: none !important;
}
div[class*="st-key-predictive_box"] button:hover {
    color: #d3a94a !important;
    background: rgba(255, 255, 255, 0.05) !important;
}
div[class*="st-key-predictive_box"] button:focus {
    color: #d3a94a !important;
    background: transparent !important;
}
</style>
""", unsafe_allow_html=True)

    picked = None
    with st.container(key=f"predictive_box_{key_prefix}"):
        st.caption(f"Suggestions for “{_html.escape(q)}”")
        for i, r in enumerate(results):
            if st.button(f"{r['label']} — {r['sub']}", key=f"pred_{key_prefix}_{i}"):
                picked = (r["label"], r["sub"])
    return picked