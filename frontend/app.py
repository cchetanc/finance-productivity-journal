import base64
import html
import re
import datetime
import textwrap
import uuid
from urllib.parse import urlparse, quote
import streamlit as st
import streamlit.components.v1 as components
import requests

from auth_helper import login_widget, is_logged_in, logout, get_role, get_id_token, get_features

try:
    import ephem
except ImportError:
    ephem = None

try:
    from streamlit_js_eval import get_geolocation
except ImportError:
    get_geolocation = None

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Finance Intelligence Journal", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700;900&family=JetBrains+Mono:wght@400;500;600&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    background-color: #15120e !important;
    color: #e8ddc7 !important;
}
header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { padding: 0 26px 26px 26px !important; max-width: 100% !important; }
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
    transition: all 0.18s ease !important;
    line-height: 1.4 !important;
    min-height: 0 !important;
    height: auto !important;
}
[data-testid="stButton"] > button:hover {
    border-color: #d3a94a !important;
    color: #d3a94a !important;
}
iframe { display: block; }
</style>
""", unsafe_allow_html=True)



# ── AUTH GATE (app-wide — not just the trade terminal) ─────────────────────────
# Every page in this app requires a signed-in @gmail.com account. Sign-in AND
# self-serve sign-up both happen here, right at the top of the app, via
# Firebase Auth (Identity Toolkit REST API) — see frontend/auth_helper.py.
if not login_widget(BACKEND_URL):
    st.stop()

# ── PER-USER FEATURE FLAGS (Admin RBAC — see pages/6_admin.py) ──────────────
# Controls which of the sections below even render for this account.
# daily_productivity defaults True for everyone; the rest default off until
# an admin switches them on for a given user.
_features = get_features()

# ── SESSION STATE ──────────────────────────────────────────────────────────────
for key in ("v_mood", "v_news", "v_headlines", "v_entertainment"):
    if key not in st.session_state:
        st.session_state[key] = 0

# ── TITHI / PANCHANG (computed locally — no external API, approximate) ──────
_TITHI_NAMES = [
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi",
    "Saptami", "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi",
    "Trayodashi", "Chaturdashi",
]

def get_tithi(dt_utc: datetime.datetime) -> str:
    """Approximate lunar day (Tithi) from geocentric Sun/Moon ecliptic longitude.
    Astronomically derived, not sourced from a published Panchang — treat as
    indicative, not authoritative for religious observance."""
    if ephem is None:
        return "Tithi unavailable"
    obs = ephem.Observer()
    obs.date = dt_utc
    moon, sun = ephem.Moon(obs), ephem.Sun(obs)
    moon.compute(obs)
    sun.compute(obs)
    moon_lon = ephem.Ecliptic(moon).lon * 180 / ephem.pi
    sun_lon = ephem.Ecliptic(sun).lon * 180 / ephem.pi
    diff = (moon_lon - sun_lon) % 360
    n = int(diff // 12)  # 0..29
    if n == 14:
        return "Purnima (Full Moon)"
    if n == 29:
        return "Amavasya (New Moon)"
    paksha = "Shukla" if n < 15 else "Krishna"
    name = _TITHI_NAMES[n if n < 15 else n - 15]
    return f"{paksha} {name}"

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_now_ist = datetime.datetime.now(IST)
_tithi_str = get_tithi(datetime.datetime.now(datetime.timezone.utc))
_date_str = _now_ist.strftime("%A, %d %B %Y")
# ISO date key (IST), same format backend/app/market_briefing.py's
# today_ist_str() uses — the shared "which calendar day is this" key for
# the Daily Productivity Assistant's persisted chat (see _load_daily_chat).
_today_ist_key = _now_ist.strftime("%Y-%m-%d")

# Gold/Silver pull in ahead of the header render since the header itself is
# a single static components.html block — see fetch_precious_metals below
# (defined here, before first use, rather than down with the other
# fetchers, purely because the header needs it earlier in the file).
@st.cache_data(ttl=120)
def fetch_precious_metals(v: int):
    try:
        r = requests.get(f"{BACKEND_URL}/api/market/precious-metals", timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"gold_inr_10g": None, "silver_inr_kg": None}

if "v_metals" not in st.session_state:
    st.session_state.v_metals = 0
_metals = fetch_precious_metals(st.session_state.v_metals)
_gold_str = f"₹{_metals['gold_inr_10g']:,.0f}/10g" if _metals.get("gold_inr_10g") else "Unavailable"
_silver_str = f"₹{_metals['silver_inr_kg']:,.0f}/kg" if _metals.get("silver_inr_kg") else "Unavailable"

# ── HEADER ────────────────────────────────────────────────────────────────────
components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Inter:wght@500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
@keyframes pulse{{0%,100%{{opacity:1;box-shadow:0 0 6px #8fae64;}}50%{{opacity:0.45;box-shadow:0 0 2px #8fae64;}}}}
.fj-header {{
    background:linear-gradient(180deg,#1c1710,#19140e);
    border-bottom:1px solid #332b1f;
    padding:16px 28px;
    display:flex;align-items:center;justify-content:space-between;
}}
.fj-brand {{ display:flex; align-items:center; gap:13px; }}
.fj-brand-name {{
    font-family:'Fraunces',serif; font-weight:600; font-size:19px;
    letter-spacing:0.3px; color:#f6efdc;
}}
.fj-tag {{ font-family:'Inter',sans-serif; font-size:10px; color:#7d6e50; letter-spacing:1.8px; text-transform:uppercase; margin-top:1px; }}
.fj-datebar {{ display:flex; align-items:center; gap:16px; }}
.fj-pill {{
    display:flex; flex-direction:column; align-items:flex-end;
    padding:6px 16px; border-left:1px solid #332b1f;
}}
.fj-pill:first-child {{ border-left:none; }}
.fj-pill-label {{ font-family:'Inter',sans-serif; font-size:9px; color:#7d6e50; letter-spacing:1.4px; text-transform:uppercase; margin-bottom:3px; }}
.fj-pill-value {{ font-family:'JetBrains Mono',monospace; font-size:13px; color:#e8ddc7; font-weight:500; }}
#fj-clock {{ color:#d3a94a; }}
</style>
<div class="fj-header">
    <div class="fj-brand">
        <div style="width:8px;height:8px;background:#8fae64;border-radius:50%;animation:pulse 2s infinite;flex-shrink:0;"></div>
        <div>
            <div class="fj-brand-name">Finance Intelligence Journal</div>
            <div class="fj-tag">Gemini Native &nbsp;·&nbsp; NSE / BSE Live</div>
        </div>
    </div>
    <div class="fj-datebar">
        <div class="fj-pill">
            <div class="fj-pill-label">Gold (spot, 10g)</div>
            <div class="fj-pill-value" style="color:#d3a94a;">{_gold_str}</div>
        </div>
        <div class="fj-pill">
            <div class="fj-pill-label">Silver (spot, kg)</div>
            <div class="fj-pill-value" style="color:#b8b8b8;">{_silver_str}</div>
        </div>
        <div class="fj-pill">
            <div class="fj-pill-label">Today</div>
            <div class="fj-pill-value">{_date_str}</div>
        </div>
        <div class="fj-pill">
            <div class="fj-pill-label">IST</div>
            <div class="fj-pill-value" id="fj-clock">--:--:--</div>
        </div>
        <div class="fj-pill">
            <div class="fj-pill-label">Tithi</div>
            <div class="fj-pill-value">{_tithi_str}</div>
        </div>
    </div>
</div>
<script>
function fjTick() {{
    const d = new Date(new Date().getTime() + (5.5*60 - new Date().getTimezoneOffset())*60000);
    const p = n => String(n).padStart(2,'0');
    const el = document.getElementById('fj-clock');
    if (el) el.textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}}
fjTick(); setInterval(fjTick, 1000);
</script>
""", height=76)

# ── ACCOUNT BAR ─────────────────────────────────────────────────────────────────
_is_admin_user = get_role() == "admin"
if _is_admin_user:
    _acct_l, _acct_mid, _acct_r = st.columns([7, 1.6, 1.4])
    with _acct_mid:
        # Only entry point into pages/6_admin.py from the home page — the
        # page itself is reachable by URL too, but without this link an
        # admin has no way to discover it short of already knowing the path.
        st.page_link("pages/6_admin.py", label="🛡️ Admin", help="Control what each user's home page shows")
else:
    _acct_l, _acct_r = st.columns([8, 1.4])
with _acct_l:
    _role_tag = " · Admin" if _is_admin_user else ""
    st.caption(f"Signed in as {st.session_state.get('fb_email', '')}{_role_tag}")
with _acct_r:
    if st.button("Sign out", key="app_signout"):
        logout()
        st.rerun()

# ── DATA FETCHERS ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def fetch_mood(v: int):
    try:
        r = requests.get(f"{BACKEND_URL}/api/market/mood", timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"systemic_score": 50, "bias": "NEUTRAL", "macro_risk_flags": ["Unable to fetch live data"], "indices": [], "index_groups": {}}

@st.cache_data(ttl=90)
def fetch_news_raw(v: int):
    try:
        r = requests.get(f"{BACKEND_URL}/api/market/news", timeout=12)
        if r.status_code == 200:
            return r.json().get("headlines", [])
    except Exception:
        pass
    return []

@st.cache_data(ttl=300)
def fetch_sentiments(v: int, titles: tuple):
    """FinBERT sentiment — cached 5 min."""
    try:
        payload = "|||".join(titles)
        r = requests.get(
            f"{BACKEND_URL}/api/market/news/sentiment",
            params={"titles": payload},
            timeout=60
        )
        if r.status_code == 200:
            return r.json().get("sentiments", {})
    except Exception:
        pass
    return {}

@st.cache_data(ttl=300)
def fetch_categorized_news(v: int, city: str):
    """Global/India/Local general-news headlines — cached 5 min, keyed by
    city too so switching locations (or having none yet) doesn't serve a
    stale 'local' tier from someone else's earlier lookup."""
    try:
        params = {"city": city} if city else {}
        r = requests.get(f"{BACKEND_URL}/api/market/news/categorized", params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"global": [], "india": [], "local": []}

@st.cache_data(ttl=1800)
def fetch_entertainment(v: int):
    """Movie/series releases — theatres + OTT (Netflix, Prime, Hotstar,
    Zee5) — cached 30 min since these don't change minute to minute."""
    try:
        r = requests.get(f"{BACKEND_URL}/api/market/entertainment", timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"theatres": [], "ott": []}

# ── LOAD DATA ──────────────────────────────────────────────────────────────────
mood_data    = fetch_mood(st.session_state.v_mood)
score        = mood_data.get("systemic_score", 50)
bias         = mood_data.get("bias", "NEUTRAL")
flags        = mood_data.get("macro_risk_flags", [])
indices      = mood_data.get("indices", [])
index_groups = mood_data.get("index_groups", {})
sc           = "#c16b57" if score < 35 else ("#d3a94a" if score < 65 else "#8fae64")

news_items   = fetch_news_raw(st.session_state.v_news)
sent_map     = {}
if news_items:
    titles_t = tuple(n["title"] for n in news_items)
    with st.spinner("FinBERT scoring market sentiment..."):
        sent_map = fetch_sentiments(st.session_state.v_news, titles_t)
for n in news_items:
    n["sentiment"] = sent_map.get(n["title"], "NEUTRAL")

# ── LIVE TICKER STRIP ─────────────────────────────────────────────────────────
_ticker_syms = ""
for idx in indices[:8]:
    if not idx or idx.get("price", 0) == 0:
        continue
    pos = idx.get("positive", False)
    col = "#8fae64" if pos else "#c16b57"
    arrow = "&#9650;" if pos else "&#9660;"
    _ticker_syms += (
        f'<span style="padding:0 22px;">{html.escape(idx["name"])}&nbsp; '
        f'{idx["price"]:,.2f}&nbsp; '
        f'<span style="color:{col};">{arrow}{abs(idx.get("change_pct", 0)):.2f}%</span></span>'
    )
if not _ticker_syms:
    _ticker_syms = '<span style="padding:0 22px;color:#7d6e50;">Ticker unavailable — try refresh</span>'

_ticker_dur = max(len(indices[:8]) * 5, 20)
components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
@keyframes tickerScroll{{0%{{transform:translateX(0);}}100%{{transform:translateX(-50%);}}}}
.ticker-box{{background:#1c1710;border-bottom:1px solid #332b1f;overflow:hidden;white-space:nowrap;padding:7px 0;}}
.ticker-track{{display:inline-block;animation:tickerScroll {_ticker_dur}s linear infinite;font-family:'JetBrains Mono',monospace;font-size:12px;color:#e8ddc7;}}
</style>
<div class="ticker-box"><div class="ticker-track">{_ticker_syms}{_ticker_syms}</div></div>
""", height=34)

# ── ROW 1: SENTIMENT PANEL + LIVE WIRE + EQUITY SIDE ─────────────────────────
# Each panel is its own function now (rather than a bare `with col:` block)
# so it can be skipped entirely, per-user, based on the admin-configured
# feature flags (see auth_helper.get_features / the Admin RBAC page) —
# the surrounding st.columns() layout is only built with the slots that
# are actually enabled, right below these three defs.
def _render_sentiment_panel():
    hdr, btn = st.columns([6, 1])
    with hdr:
        st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#a99872;text-transform:uppercase;margin:0 0 5px 0;">◎ Live Market Sentiment</p>', unsafe_allow_html=True)
    with btn:
        if st.button("↺", key="ref_mood"):
            st.session_state.v_mood += 1
            st.cache_data.clear()
            st.rerun()

    flags_html = "".join([
        f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:8px;">'
        f'<div style="width:4px;height:4px;background:{sc};border-radius:50%;flex-shrink:0;"></div>'
        f'<span style="font-size:11.5px;color:#e8ddc7;font-weight:500;">{f}</span></div>'
        for f in (flags or ["No risk flags"])
    ])
    components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:0.3;}}}}</style>
<div style="background:#211b13;border:1px solid #332b1f;border-radius:8px;padding:20px 22px;height:258px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #332b1f;padding-bottom:15px;margin-bottom:14px;">
        <div>
            <div style="font-size:10px;color:#a99872;text-transform:uppercase;font-weight:600;letter-spacing:1px;margin-bottom:5px;">Quantitative Bias</div>
            <div style="font-size:26px;font-weight:900;color:{sc};letter-spacing:0.5px;">{bias}</div>
        </div>
        <div style="text-align:right;">
            <div style="font-size:10px;color:#a99872;text-transform:uppercase;font-weight:600;letter-spacing:1px;margin-bottom:5px;">Macro Score</div>
            <div><span style="font-size:44px;font-weight:900;color:{sc};line-height:1;">{score}</span><span style="font-size:16px;color:#a99872;font-weight:600;"> / 100</span></div>
        </div>
    </div>
    <div style="font-size:10px;color:#a99872;text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:10px;">Live Risk Flags</div>
    {flags_html}
</div>
""", height=276)

def _render_live_wire():
    hdr, btn = st.columns([6, 1])
    with hdr:
        st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#a99872;text-transform:uppercase;margin:0 0 5px 0;">((•)) Live Wire — FinBERT Scored</p>', unsafe_allow_html=True)
    with btn:
        if st.button("↺", key="ref_news"):
            st.session_state.v_news += 1
            st.cache_data.clear()
            st.rerun()

    display_news = news_items[:12]
    looped       = display_news * 3 if display_news else []
    anim_dur     = max(len(display_news) * 6, 30)

    rows_html = ""
    for n in looped:
        sent  = n.get("sentiment", "NEUTRAL").upper()

        # news_items comes from our own backend, which in turn scrapes external
        # news sources — treat title/link/source/published as untrusted input.
        # They get rendered inside components.html, which Streamlit puts in an
        # iframe sandboxed with "allow-scripts allow-same-origin" (Streamlit's
        # own default since 0.73 — not something we control), so anything that
        # isn't fully escaped here can execute as live JS in that context.
        raw_link = n.get("link", "") or ""
        parsed   = urlparse(raw_link)
        link     = html.escape(raw_link, quote=True) if parsed.scheme in ("http", "https") else "#"
        title    = html.escape(n.get("title", ""), quote=True)
        src      = html.escape(n.get("source", "")[:20], quote=True)
        pub      = html.escape(n.get("published", ""), quote=True)

        if sent == "BULLISH":
            sym, s_col, s_bg = "▲", "#8fae64", "rgba(143,174,100,0.10)"
        elif sent == "BEARISH":
            sym, s_col, s_bg = "▼", "#c16b57", "rgba(193,107,87,0.10)"
        else:
            sym, s_col, s_bg = "●", "#a99872", "transparent"

        rows_html += f"""
<div style="padding:8px 12px 8px 13px;margin-bottom:3px;border-left:3px solid {s_col};background:{s_bg};">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
        <span style="font-size:14px;color:{s_col};font-weight:900;line-height:1;">{sym}</span>
        <span style="font-size:9px;font-weight:700;color:{s_col};font-family:monospace;border:1px solid {s_col};padding:1px 5px;border-radius:2px;">{sent}</span>
        <span style="font-size:9px;color:#7d6e50;font-family:monospace;">{src} · {pub}</span>
    </div>
    <a href="{link}" target="_blank"
       style="font-size:12.5px;color:#e8ddc7;text-decoration:none;line-height:1.5;font-weight:500;display:block;"
       onmouseover="this.style.color='#d3a94a'" onmouseout="this.style.color='#e8ddc7'">{title}</a>
</div>"""

    if not rows_html:
        rows_html = '<div style="padding:20px 0;color:#7d6e50;font-size:13px;">No live news. Try ↺ refresh.</div>'

    components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:0.35;}}}}
@keyframes scrollUp{{0%{{transform:translateY(0);}}100%{{transform:translateY(-33.333%);}}}}
.wbox{{background:#211b13;border:1px solid #332b1f;border-radius:8px;padding:14px 16px;height:258px;overflow:hidden;position:relative;}}
.wbox::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:55px;background:linear-gradient(to top,#211b13 40%,transparent);z-index:1;pointer-events:none;border-radius:0 0 8px 8px;}}
.wtrack{{animation:scrollUp {anim_dur}s linear infinite;will-change:transform;}}
</style>
<div class="wbox">
    <div class="wtrack">{rows_html}</div>
</div>
""", height=276)

def _render_equity_side():
    st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#a99872;text-transform:uppercase;margin:0 0 5px 0;">📈 Equity Side</p>', unsafe_allow_html=True)
    st.markdown("""
<style>
/* Reskins the native st.container/st.page_link below to match the dark
   card look used by the Live Market Sentiment / Live Wire panels either
   side of it — those are custom components.html blocks, but st.page_link
   has to stay a real Streamlit element for in-app navigation to work, so
   this scopes CSS to the container's `key` instead (same technique as the
   CFA assistant panel above). */
div[class*="st-key-equity_side_box"] {
    background: #211b13 !important;
    border: 1px solid #332b1f !important;
    border-radius: 8px !important;
    padding: 16px 14px 10px 14px !important;
    height: 258px !important;
    overflow-y: auto !important;
}
div[class*="st-key-equity_side_box"] > div {
    height: 100% !important;
}
div[class*="st-key-equity_side_box"] [data-testid="stCaptionContainer"] p {
    color: #7d6e50 !important;
    font-size: 11px !important;
    line-height: 1.4 !important;
    margin-bottom: 10px !important;
}
div[class*="st-key-equity_side_box"] a[data-testid="stPageLink-NavLink"] {
    background: transparent !important;
    border: 1px solid #332b1f !important;
    border-radius: 6px !important;
    padding: 7px 10px !important;
    margin-bottom: 6px !important;
    transition: all 0.15s ease !important;
}
div[class*="st-key-equity_side_box"] a[data-testid="stPageLink-NavLink"]:hover {
    border-color: #d3a94a !important;
    background: rgba(211,169,74,0.06) !important;
}
div[class*="st-key-equity_side_box"] a[data-testid="stPageLink-NavLink"] p {
    color: #e8ddc7 !important;
    font-size: 12.5px !important;
    font-weight: 500 !important;
}
</style>
""", unsafe_allow_html=True)
    with st.container(key="equity_side_box"):
        st.caption("Deep-dive screens — separate pages, not live-fetched here.")
        st.page_link("pages/1_Equity_Screener.py", label="Equity Screener", icon="📊")
        st.page_link("pages/2_Mutual_Funds.py", label="Mutual Funds", icon="💰")
        st.page_link("pages/3_Dividends_and_Corporate_Actions.py", label="Dividends & Corp Actions", icon="📢")
        st.page_link("pages/4_Results_Calendar.py", label="Results Calendar", icon="📅")
        # Trade Terminal used to render inline, full-width, right below this
        # row — now it's just another Equity Side destination, so it only
        # loads when someone actually wants it (see pages/5_trade_terminal.py).
        st.page_link("pages/5_trade_terminal.py", label="Trade Terminal", icon="⚡")


# ── Build row 1 with only the panels this user is allowed to see ────────────
# Each entry is (render_fn, relative_width). equity_news controls the
# sentiment + live-wire pair; smart_investor controls the equity side box —
# they're independent, so any combination (both, either, neither) is valid,
# and the st.columns() call below only ever gets the slots that are on.
_row1_parts = []
if _features.get("equity_news"):
    _row1_parts.append((_render_sentiment_panel, 1))
    _row1_parts.append((_render_live_wire, 1.5))
if _features.get("smart_investor"):
    _row1_parts.append((_render_equity_side, 0.9))

if _row1_parts:
    _row1_cols = st.columns([w for _, w in _row1_parts])
    for (_fn, _w), _col in zip(_row1_parts, _row1_cols):
        with _col:
            _fn()

# Four small, independent tiles instead of one wide combined card: three
# equal-width news tiles (Global, National, Local) plus a compact "New
# Releases" tile covering theatres and OTT platforms (Netflix, Prime,
# JioHotstar/Disney+Hotstar, Zee5).
st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

_TILE_FONT = "https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap"
_TILE_H = 168  # inner card height; components.html height adds a little chrome


def _headline_rows(items: list, limit: int = 3) -> str:
    if not items:
        return '<div style="color:#6b5c40;font-size:11px;padding:4px 0;">No headlines right now.</div>'
    rows = ""
    for h in items[:limit]:
        # Same scheme-allowlist pattern as the Live Wire panel above — these
        # links come from external RSS feeds via our own backend, rendered
        # inside components.html's sandboxed iframe, so anything not fully
        # escaped/validated here could execute as live JS in that context.
        raw_link = h.get("link", "") or ""
        parsed   = urlparse(raw_link)
        link     = html.escape(raw_link, quote=True) if parsed.scheme in ("http", "https") else "#"
        title    = html.escape(h.get("title") or "")
        source   = html.escape(h.get("source") or "")
        rows += f'''
<div style="margin-bottom:7px;">
    <a href="{link}" target="_blank" rel="noopener"
       style="font-size:11.5px;line-height:1.35;color:#e8ddc7;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block;"
       onmouseover="this.style.color='#d3a94a';this.style.textDecoration='underline'" onmouseout="this.style.color='#e8ddc7';this.style.textDecoration='none'">{title}</a>
    <div style="font-size:9.5px;color:#7d6e50;margin-top:1px;">{source} · {h.get("published","")}</div>
</div>'''
    return rows


def _news_tile(container, icon: str, label: str, rows_html: str, key_suffix: str):
    with container:
        hdr, btn = st.columns([7, 1])
        with hdr:
            st.markdown(f'<p style="font-size:9.5px;font-weight:700;letter-spacing:1px;color:#a99872;text-transform:uppercase;margin:0 0 5px 0;">{icon} {label}</p>', unsafe_allow_html=True)
        with btn:
            if st.button("↺", key=f"ref_{key_suffix}"):
                st.session_state.v_headlines += 1
                st.rerun()
        components.html(f"""
<link href="{_TILE_FONT}" rel="stylesheet">
<div style="background:#211b13;border:1px solid #332b1f;border-radius:8px;padding:12px 14px;height:{_TILE_H}px;font-family:'Inter',sans-serif;overflow:hidden;">
    {rows_html}
</div>
""", height=_TILE_H + 18)


_news_city = st.session_state.get("user_location") or ""

if _features.get("news"):
    _news = fetch_categorized_news(st.session_state.v_headlines, _news_city)
    _local_rows = _headline_rows(_news.get("local")) if _news_city else (
        '<div style="color:#6b5c40;font-size:11px;padding:4px 0;">Enable location for local news.</div>'
    )

    col_global, col_national, col_local = st.columns(3)
    _news_tile(col_global, "🌍", "Global", _headline_rows(_news.get("global")), "global")
    _news_tile(col_national, "🇮🇳", "National", _headline_rows(_news.get("india")), "national")
    _news_tile(col_local, "📍", "Local", _local_rows, "local")

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

# ── NEW RELEASES: THEATRES + OTT (Netflix / Prime / Hotstar / Zee5) ─────────
if _features.get("entertainment"):
    hdr_ent, btn_ent = st.columns([10, 1])
    with hdr_ent:
        st.markdown('<p style="font-size:9.5px;font-weight:700;letter-spacing:1px;color:#a99872;text-transform:uppercase;margin:0 0 5px 0;">🎬 New Releases · Theatres &amp; OTT</p>', unsafe_allow_html=True)
    with btn_ent:
        if st.button("↺", key="ref_entertainment"):
            st.session_state.v_entertainment += 1
            st.rerun()

    _ent = fetch_entertainment(st.session_state.v_entertainment)
    _ent_empty = '<div style="color:#6b5c40;font-size:11px;padding:4px 0;">Nothing new to report.</div>'

    components.html(f"""
<link href="{_TILE_FONT}" rel="stylesheet">
<div style="background:#211b13;border:1px solid #332b1f;border-radius:8px;padding:12px 14px 14px 14px;font-family:'Inter',sans-serif;display:grid;grid-template-columns:1fr 1fr;gap:14px;overflow:hidden;">
    <div>
        <div style="font-size:9px;color:#a99872;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;padding-bottom:5px;margin-bottom:6px;border-bottom:1px solid #2d2617;">🎟️ In Theatres</div>
        {_headline_rows(_ent.get("theatres")) or _ent_empty}
    </div>
    <div>
        <div style="font-size:9px;color:#a99872;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;padding-bottom:5px;margin-bottom:6px;border-bottom:1px solid #2d2617;">📺 Netflix · Prime · Hotstar · Zee5</div>
        {_headline_rows(_ent.get("ott")) or _ent_empty}
    </div>
</div>
""", height=_TILE_H + 18)

if _features.get("news") or _features.get("entertainment"):
    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)


# ── FLOATING CFA ASSISTANT PANEL ─────────────────────────────────────────────
# Rebuilt on a manually-toggled st.container (targeted via its `key`, which
# Streamlit exposes as a `st-key-<key>` CSS class) instead of st.popover.
# st.popover hard-caps its body to a small anchored dropdown — that 380px
# forced width above was the actual source of the cramped feel, not
# something that could be fixed by tweaking padding. A key-targeted
# container has no such ceiling, so this can be a real, wide reading pane.
CFA_DISCLAIMER = (
    "I am an AI assistant, not a licensed financial advisor. Information here "
    "is for educational purposes only and does not constitute financial or "
    "trading advice. Execute trades at your own risk."
)
# Backend prepends its own copy of this line to every single reply and the
# router sometimes leaves a raw "[equity_agent Insight]:" style label at the
# front of a single-agent answer — both are meant for logs/attribution, not
# for a reader scanning a chat thread. Strip them client-side so the panel
# shows a clean line of dialogue and the disclaimer is stated once, not once
# per message.
_AGENT_LABEL_RE = re.compile(r"^\[[\w\s]+(Insight|Error)\]:\s*\n?")
_DISCLAIMER_RE = re.compile(r"^DISCLAIMER:.*?(?:\n\n|\n(?=\S))", re.DOTALL)


def _clean_cfa_text(raw: str) -> str:
    text = _DISCLAIMER_RE.sub("", raw or "", count=1).strip()
    text = _AGENT_LABEL_RE.sub("", text, count=1).strip()
    return text or raw


if "cfa_panel_open" not in st.session_state:
    # Now an inline, always-in-flow section (it lives where the Trade
    # Terminal used to sit) rather than a floating overlay, so it starts
    # open by default — no click needed to discover it.
    st.session_state.cfa_panel_open = True
if "voice_history" not in st.session_state:
    st.session_state.voice_history = []
if "last_audio_id" not in st.session_state:
    st.session_state.last_audio_id = None
if "greeted" not in st.session_state:
    st.session_state.greeted = False
if "user_location" not in st.session_state:
    st.session_state.user_location = None
if "location_attempts" not in st.session_state:
    st.session_state.location_attempts = 0
if "pending_cfa_query" not in st.session_state:
    st.session_state.pending_cfa_query = None
if "chat_loaded_date" not in st.session_state:
    # IST calendar-date key of whatever's currently loaded into
    # voice_history — compared against "today" every rerun so a midnight
    # rollover clears the panel and re-fetches a fresh day (see the
    # daily-chat load block below). None means nothing's been loaded yet.
    st.session_state.chat_loaded_date = None

def _reverse_geocode(lat: float, lon: float) -> str:
    """Free, key-less reverse geocode via OpenStreetMap Nominatim — turns raw
    coordinates into a place name (e.g. 'Gachibowli, Hyderabad') that's far
    more useful in a web-search query than bare lat/lon. Falls back to the
    raw coordinates if the lookup fails; Nominatim's usage policy requires a
    real User-Agent and reasonable (non-bulk) call volume, which a one-shot
    per-session lookup like this respects."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2"},
            headers={"User-Agent": "finance-productivity-journal/1.0"},
            timeout=6,
        )
        if resp.status_code == 200:
            addr = resp.json().get("address", {})
            parts = [
                addr.get("suburb") or addr.get("neighbourhood") or addr.get("road"),
                addr.get("city") or addr.get("town") or addr.get("state_district"),
                addr.get("state"),
            ]
            place = ", ".join(p for p in parts if p)
            if place:
                return place
    except Exception:
        pass
    return f"{lat:.4f},{lon:.4f}"


# Geolocation fetch — MUST be called unconditionally, every single rerun,
# at the same point in the script. streamlit_js_eval's own docs list
# "calling SJE from inside a branch (if/loop)" as a known limitation: it
# uses a call-order-based hook system, so wrapping the call itself in a
# condition that changes across reruns (as this used to: gated on
# cfa_panel_open / not-yet-set / attempts-remaining) desyncs its internal
# state and it silently never reports the resolved value back — which
# matches exactly what was observed: the browser's permission toggle shows
# granted, but session_state.user_location never gets set. Branching is now
# only on what to DO with the result, never on whether to make the call.
_geo_result = get_geolocation(component_key="cfa_geo_lookup") if get_geolocation else None
if isinstance(_geo_result, dict):
    if "coords" in _geo_result and not st.session_state.user_location:
        st.session_state.user_location = _reverse_geocode(
            _geo_result["coords"]["latitude"], _geo_result["coords"]["longitude"]
        )
    elif "error" in _geo_result:
        # Permission denied / unsupported / timed out — stop expecting a
        # result so the UI can fall back to asking the user directly.
        st.session_state.location_attempts = 5


# ── Fallback greeting, used ONLY if the backend's phase-aware /daily-chat
# endpoint can't be reached (e.g. connection error). The real greeting —
# a data-grounded pre-market read, a live intraday update, an end-of-day
# wrap-up, or a weekend redirect — is generated server-side by
# backend/app/market_briefing.py and fetched via _load_daily_chat below,
# so it reflects actual market factors instead of a canned line.
_FALLBACK_GREETING = (
    "Here for the session, or here for something else today? Either way, "
    "I've got you — market movers, a specific stock, or just a good movie "
    "recommendation, your call."
)


def _load_daily_chat() -> None:
    """Fetches today's Daily Productivity Assistant chat from the backend
    (backend/app/routers/market.py's GET /api/market/daily-chat) and loads
    it into voice_history. That endpoint is phase-aware (pre-market /
    market-hours / post-market / weekend) and only generates a NEW
    proactive message the first time a given phase is seen for the day —
    reconnecting again later returns exactly what's already there, so the
    panel never repeats itself. Logged-in users get the full day's chat
    persisted server-side in Firestore (survives a reconnect); everyone
    else still gets the phase-aware greeting, just without cross-session
    persistence. A fresh IST calendar day always starts empty."""
    try:
        params = {}
        _token = get_id_token()
        if _token:
            params["id_token"] = _token
        resp = requests.get(f"{BACKEND_URL}/api/market/daily-chat", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        st.session_state.voice_history = [
            {"role": m["role"], "text": m["text"], "route": m.get("route")}
            for m in data.get("messages", [])
        ]
        st.session_state.chat_loaded_date = data.get("date") or _today_ist_key
    except Exception:
        # Backend unreachable — still show something rather than an empty
        # panel, but don't mark a date as "loaded" so the next successful
        # rerun retries the real fetch instead of getting stuck on this.
        st.session_state.voice_history = [{"role": "cfa", "text": _FALLBACK_GREETING}]

st.markdown("""
<style>
/* Inline toggle — sits at the top of the panel itself now (it used to be
   a fixed top-right pill for a floating overlay; the panel is a normal,
   full-width section in the page flow now, right where the Trade
   Terminal used to be, so the toggle just collapses/expands it in place). */
div[class*="st-key-cfa_toggle_wrap"] button {
    border-radius: 20px !important;
    padding: 6px 16px !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    background: #211b13 !important;
    border: 1px solid #463b28 !important;
    color: #e8ddc7 !important;
    transition: all 0.2s ease !important;
}
div[class*="st-key-cfa_toggle_wrap"] button:hover {
    border-color: #8fae64 !important;
    color: #8fae64 !important;
}

/* The panel itself — now a broad, normal-flow section (same width as the
   rest of the dashboard) instead of a fixed slide-in overlay. */
div[class*="st-key-cfa_panel"] {
    background: #1c1710 !important;
    border: 1px solid #463b28 !important;
    border-radius: 10px !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35) !important;
    padding: 22px 28px 20px 28px !important;
}
.cfa-disclaimer {
    font-size: 11px;
    color: #8f7f5d;
    line-height: 1.5;
    font-style: italic;
    border-bottom: 1px solid #332b1f;
    padding-bottom: 14px;
    margin-bottom: 16px;
}
.cfa-bubble-row { display: flex; align-items: flex-end; gap: 8px; margin-bottom: 16px; }
.cfa-avatar {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10.5px;
    font-weight: 700;
    font-family: 'Inter', sans-serif;
}
.cfa-bubble {
    padding: 12px 16px;
    border-radius: 14px;
    max-width: 78%;
    font-size: 14.5px;
    line-height: 1.65;
}
/* The reply text inside .cfa-bubble is now rendered via a real st.markdown()
   call (see the chat loop) instead of flat escaped text, so bullets, bold,
   and tables all render — these rules just strip Streamlit's default block
   spacing/dark-mode-agnostic table styling so it still reads as one tight
   chat bubble instead of a normal page section. */
.cfa-bubble div[data-testid="stMarkdownContainer"] { margin: 0; }
.cfa-bubble div[data-testid="stMarkdownContainer"] > *:first-child { margin-top: 0 !important; }
.cfa-bubble div[data-testid="stMarkdownContainer"] > *:last-child { margin-bottom: 0 !important; }
.cfa-bubble p { margin: 0 0 8px 0; }
.cfa-bubble ul, .cfa-bubble ol { margin: 4px 0 8px 0; padding-left: 20px; }
.cfa-bubble li { margin-bottom: 3px; }
.cfa-bubble strong { color: #f6efdc; }
.cfa-bubble table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    margin: 6px 0 10px 0;
}
.cfa-bubble th, .cfa-bubble td {
    border: 1px solid rgba(143,174,100,0.25);
    padding: 5px 9px;
    text-align: left;
}
.cfa-bubble th { background: rgba(143,174,100,0.15); color: #f6efdc; font-weight: 700; }
.cfa-empty-state {
    text-align: center;
    color: #6b5c40;
    font-size: 13px;
    padding: 48px 12px;
}
</style>
""", unsafe_allow_html=True)

_daily_agent_enabled = _features.get("daily_productivity", True)

if _daily_agent_enabled:
    toggle_l, toggle_r = st.columns([9, 2])
    with toggle_l:
        st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#a99872;text-transform:uppercase;margin:10px 0 5px 0;">🤖 Daily Productivity Assistant</p>', unsafe_allow_html=True)
    with toggle_r:
        with st.container(key="cfa_toggle_wrap"):
            toggle_label = "▲ Collapse" if st.session_state.cfa_panel_open else "▼ Expand"
            if st.button(toggle_label, key="cfa_toggle_btn"):
                st.session_state.cfa_panel_open = not st.session_state.cfa_panel_open

# Greeting + GPS fetch run AFTER the toggle above has settled this run's
# final open/closed state — doing this before the toggle block (as an
# earlier version did) meant the very click that opened the panel was
# evaluated against the *stale* pre-click state, so the greeting only
# ever appeared one interaction later (e.g. after typing "hi").
# Loads (once) whenever the panel is open and either nothing's been loaded
# yet this session, or the IST calendar day has rolled over since the last
# load — which is what makes "today's messages stick around all day, but a
# new day starts fresh" work: chat_loaded_date is the persisted document's
# own date key, and a mismatch means we're now into a new one.
if _daily_agent_enabled and st.session_state.cfa_panel_open and st.session_state.chat_loaded_date != _today_ist_key:
    _load_daily_chat()
    st.session_state.greeted = True

if _daily_agent_enabled and st.session_state.cfa_panel_open:
    @st.fragment
    def render_cfa_panel():
        with st.container(key="cfa_panel"):
            st.markdown(
                '<div style="font-size:13px;font-weight:900;letter-spacing:1px;color:#f6efdc;'
                'text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px;">'
                '<span style="font-size:18px;">🤖</span> Daily Productivity Assistant</div>',
                unsafe_allow_html=True
            )
            st.markdown(f'<div class="cfa-disclaimer">{html.escape(CFA_DISCLAIMER)}</div>', unsafe_allow_html=True)
            if st.session_state.user_location:
                st.markdown(
                    f'<div style="font-size:11px;color:#8fae64;margin:-6px 0 10px 0;">'
                    f'📍 Using your location for nearby suggestions: {html.escape(st.session_state.user_location)}</div>',
                    unsafe_allow_html=True,
                )
            elif get_geolocation is None:
                st.markdown(
                    '<div style="font-size:11px;color:#8f7f5d;margin:-6px 0 10px 0;">'
                    '📍 Location lookup unavailable — ask "movies near [your area]" instead.</div>',
                    unsafe_allow_html=True,
                )
    
            # ── Top-headlines strip removed — Global/National/Local headlines
            # now live as their own tiles on the main dashboard (Row 2), so
            # repeating them inside this chat panel was redundant.
    
            # ── Gmail connect — enables the spending_agent's "how did my
            # spending look" answers (see backend/app/gmail_spending.py). A
            # separate, explicit Google consent grant from Firebase login itself
            # — logging into this app never implies Gmail access was granted.
            with st.expander("🔗 Connect Gmail for spending insights", expanded=False):
                _qp = st.query_params
                if _qp.get("gmail_connect") == "success":
                    st.success("Gmail connected — ask about your spending any time.")
                    st.query_params.clear()
                elif _qp.get("gmail_connect") == "error":
                    st.error(f"Gmail connection failed ({_qp.get('reason', 'unknown error')}). Try again.")
                    st.query_params.clear()
    
                _token = get_id_token()
                if not _token:
                    st.caption("Log in first to connect Gmail.")
                else:
                    _headers = {"Authorization": f"Bearer {_token}"}
                    try:
                        _status = requests.get(f"{BACKEND_URL}/api/gmail/status", headers=_headers, timeout=10).json()
                    except Exception:
                        _status = {"connected": False}
    
                    if _status.get("connected"):
                        st.markdown("✅ **Gmail connected** — UPI/bank debit alerts are being scanned for spending insights.")
                        gcol1, gcol2, gcol3 = st.columns(3)
                        with gcol1:
                            if st.button("Refresh spending data", key="gmail_sync_btn"):
                                try:
                                    r = requests.post(f"{BACKEND_URL}/api/gmail/sync", headers=_headers, timeout=60)
                                    r.raise_for_status()
                                    st.success(f"Synced — {r.json().get('new_transactions_stored', 0)} new transactions found.")
                                except Exception as e:
                                    st.error(f"Sync failed: {e}")
                        with gcol2:
                            # Ordinary refresh above only picks up NEW mail —
                            # transactions already stored keep whatever merchant
                            # the parser extracted at the time, even after a
                            # parsing-logic fix. This re-parses every stored
                            # transaction with today's logic, correcting things
                            # like a helpline number or boilerplate phrase
                            # ("help you") that got mistaken for a merchant
                            # before. Slower than a normal refresh since it
                            # re-fetches every matched email, not just new ones.
                            if st.button("Fix past entries", key="gmail_reparse_btn", help="Re-parses all previously-synced transactions with the latest merchant-extraction logic — use this once if old summaries show odd merchant names like a phone number or 'help you'."):
                                try:
                                    r = requests.post(
                                        f"{BACKEND_URL}/api/gmail/sync",
                                        params={"force_reparse": "true"},
                                        headers=_headers, timeout=120,
                                    )
                                    r.raise_for_status()
                                    st.success(f"Re-parsed — {r.json().get('new_transactions_stored', 0)} transactions updated.")
                                except Exception as e:
                                    st.error(f"Re-parse failed: {e}")
                        with gcol3:
                            if st.button("Disconnect", key="gmail_disconnect_btn"):
                                try:
                                    requests.post(f"{BACKEND_URL}/api/gmail/disconnect", headers=_headers, timeout=10)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Disconnect failed: {e}")
                    else:
                        st.caption(
                            "Connect your Gmail (read-only) so I can spot UPI/bank debit alerts and "
                            "answer things like \"how did my spending go this month?\". Nothing else in "
                            "your inbox is read."
                        )
                        if st.button("Connect Gmail", key="gmail_connect_btn"):
                            try:
                                r = requests.get(f"{BACKEND_URL}/api/gmail/auth-url", headers=_headers, timeout=10)
                                r.raise_for_status()
                                auth_url = r.json().get("auth_url")
                                st.markdown(f'<a href="{auth_url}" target="_self">Click here to continue to Google →</a>', unsafe_allow_html=True)
                            except Exception as e:
                                st.error(f"Couldn't start Gmail connection: {e}")
    
            # Persona + voice-reply controls
            ctrl1, ctrl2 = st.columns([2, 1.6])
            with ctrl1:
                persona = st.selectbox(
                    "Voice", ["Aoede", "Kore", "Puck", "Charon", "Fenrir"],
                    key="cfa_persona", label_visibility="collapsed"
                )
            with ctrl2:
                speak_reply = st.toggle("🔊 Speak reply", value=False, key="cfa_speak_reply")
    
            # Auto-queue a query queued elsewhere in the app
            if st.session_state.get("pending_cfa_query"):
                _pending = st.session_state.pending_cfa_query
                st.session_state.pending_cfa_query = None
                st.session_state.voice_history.append({"role": "user", "text": _pending})
                st.session_state.pending_prompt_text = _pending
                st.rerun(scope="fragment")
    
            # ── Scrollable message history ──
            with st.container(height=500, border=False):
                if not st.session_state.voice_history:
                    st.info("Ask about a stock, fund, sector, or your portfolio — the desk is standing by.")
                
                import datetime
                
                for msg in st.session_state.voice_history:
                    is_user = (msg["role"] == "user")
                    name = "You" if is_user else "Daily Productivity Assistant"
                    bg_color = "#2a2d32" if is_user else "#1e1e24" # Dark theme card colors
                    border_color = "#3a3d42" if is_user else "#2e2e34"
                    timestamp = datetime.datetime.now().strftime("%I:%M %p") # ideally we'd store real timestamp
                    
                    # Use Streamlit's native markdown rendering by wrapping the text with blank lines inside the HTML.
                    # IMPORTANT: any HTML line indented 4+ spaces (and preceded by a blank line) gets treated by
                    # Streamlit's markdown renderer as a Markdown "indented code block", which prints the raw tags
                    # as visible text instead of rendering them as HTML — that's what was causing stray "</div>"
                    # text to show up in the chat. textwrap.dedent() fixes that, but it must run on the header/
                    # footer wrapper ONLY, before the message text is spliced in — dedent looks at every line of
                    # the final string, so if it ran on the whole f-string, a multi-line assistant reply
                    # containing any line with no leading spaces would "poison" the common-indent calculation and
                    # leave the surrounding <div> tags un-dedented again.
                    _card_header = textwrap.dedent(f"""\
                    <div style="background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 8px; padding: 16px; margin-bottom: 16px; font-family: sans-serif;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid {border_color}; padding-bottom: 8px;">
                    <div style="font-weight: 600; font-size: 13px; color: #e0e0e0;">{name}</div>
                    <div style="font-size: 11px; color: #888888;">{timestamp}</div>
                    </div>
                    <div style="font-size: 14px; line-height: 1.6; color: #d0d0d0;">
                    """)
                    _card_footer = textwrap.dedent("""
                    </div>
                    </div>
                    """)
                    _msg_text = msg["text"]
                    _chart_payload = None
                    import re, json
                    _match = re.search(r'```json\s*(\{.*?"InteractiveChart".*?\})\s*```', _msg_text, re.DOTALL)
                    if _match:
                        try:
                            _chart_payload = json.loads(_match.group(1))
                            _msg_text = _msg_text.replace(_match.group(0), "")
                        except Exception:
                            pass
                            
                    html_card = _card_header + "\n" + _msg_text + "\n" + _card_footer
                    st.markdown(html_card, unsafe_allow_html=True)
                    
                    if _chart_payload and _chart_payload.get("component") == "InteractiveChart":
                        c_type = _chart_payload.get("chartType", "line")
                        c_title = _chart_payload.get("title", "")
                        if c_title:
                            st.caption(f"**{c_title}**")
                        
                        import pandas as pd
                        df_data = {}
                        labels = []
                        for s in _chart_payload.get("series", []):
                            s_name = s.get("name", "Value")
                            s_data = s.get("data", [])
                            if not labels:
                                labels = [d.get("label", "") for d in s_data]
                            df_data[s_name] = [d.get("value", 0) for d in s_data]
                        
                        if df_data and labels:
                            df = pd.DataFrame(df_data, index=labels)
                            if c_type == "bar":
                                st.bar_chart(df)
                            else:
                                st.line_chart(df)
                    
                    # We still want to show the custom agent widgets below the text if it's the assistant
                    if not is_user:
                        _labels = (msg.get("route") or {}).get("agent_labels") or []
                        if _labels:
                            st.caption(f"**{html.escape(' + '.join(_labels))}**")
                        
                        if msg.get("audio"):
                            st.markdown(f'<audio controls autoplay style="width:100%;height:36px;border-radius:4px;"><source src="data:audio/mp3;base64,{msg["audio"]}" type="audio/mp3"></audio>', unsafe_allow_html=True)
                            
                        _route = msg.get("route")
                        if _route and _route.get("source") and _route.get("destination"):
                            _src_q = quote(_route["source"])
                            _dst_q = quote(_route["destination"])
                            _embed_url = f"https://www.google.com/maps?saddr={_src_q}&daddr={_dst_q}&output=embed"
                            components.html(f"""
    <div style="border:1px solid #3a4a2a;border-radius:10px;overflow:hidden;margin-top:10px;">
        <iframe src="{_embed_url}" width="100%" height="280" style="border:0;display:block;" loading="lazy"></iframe>
    </div>
    """, height=286)
    
                        _snap = _route.get("snapshot") if _route else None
                        if _snap:
                            def _fmt(v, suffix="", prefix="", decimals=2):
                                if v is None: return "—"
                                try: return f"{prefix}{float(v):,.{decimals}f}{suffix}"
                                except: return str(v)
    
                            _chg = _snap.get("dayChangePct")
                            _chg_color = "#8fae64" if (_chg or 0) >= 0 else "#c4685a"
                            _chg_str = f"{_chg:+.2f}%" if _chg is not None else "—"
                            _ychg = _snap.get("yearChangePct")
                            _ychg_badge = ""
                            if _ychg is not None:
                                _yc_color = "#8fae64" if _ychg >= 0 else "#c4685a"
                                _yc_arrow = "↑" if _ychg >= 0 else "↓"
                                _ychg_badge = f'<span style="font-size:11px;font-weight:700;color:{_yc_color};background:rgba(143,174,100,0.12);border-radius:20px;padding:2px 9px;margin-left:8px;">{_yc_arrow} {abs(_ychg):.2f}% past year</span>'
                                
                            st.markdown(textwrap.dedent(f'''\
                            <div style="margin-top:10px;padding:14px 16px;border:1px solid #3a3226;border-radius:10px;background:rgba(143,174,100,0.05);">
                                <div style="font-size:13px;font-weight:800;color:#f6efdc;">{html.escape(str(_snap.get("symbol") or ""))}
                                    <span style="font-weight:400;color:#a99872;">{html.escape(str(_snap.get("shortName") or ""))}</span></div>
                                <div style="font-size:22px;font-weight:800;color:#f6efdc;margin:4px 0;">
                                    ₹{_fmt(_snap.get("currentPrice"))}
                                    <span style="font-size:13px;font-weight:700;color:{_chg_color};margin-left:8px;">{_chg_str} today</span>
                                    {_ychg_badge}
                                </div>
                                <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:11px;color:#a99872;margin-top:8px;">
                                    <div>Prev close<br><span style="color:#e8ddc7;">₹{_fmt(_snap.get("previousClose"))}</span></div>
                                    <div>Day range<br><span style="color:#e8ddc7;">₹{_fmt(_snap.get("dayLow"))} – ₹{_fmt(_snap.get("dayHigh"))}</span></div>
                                    <div>52-wk range<br><span style="color:#e8ddc7;">₹{_fmt(_snap.get("week52Low"))} – ₹{_fmt(_snap.get("week52High"))}</span></div>
                                    <div>Mkt cap<br><span style="color:#e8ddc7;">₹{_fmt((_snap.get("marketCap") or 0)/1e7, decimals=0)} Cr</span></div>
                                    <div>P/E<br><span style="color:#e8ddc7;">{_fmt(_snap.get("trailingPE"))}</span></div>
                                    <div>Div yield<br><span style="color:#e8ddc7;">{_fmt(_snap.get("dividendYield"))}%</span></div>
                                </div>
                            </div>
                            '''), unsafe_allow_html=True)

    
                        _peers = _route.get("peers") if _route else None
                        if _peers and _peers.get("peers"):
                            st.caption(f'**PEERS IN {html.escape(str(_peers.get("sector") or "")).upper()}**')
                            _rows = [_peers["target"]] + _peers["peers"]
                            st.dataframe(
                                [
                                    {
                                        "Symbol": r.get("symbol"),
                                        "Price (₹)": r.get("current_price"),
                                        "Mkt Cap (₹Cr)": round((r.get("market_cap") or 0) / 1e7) if r.get("market_cap") else None,
                                        "P/E": r.get("pe_ratio"),
                                        "ROE %": r.get("roe"),
                                    }
                                    for r in _rows
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )
    
                        _movie = _route.get("movie_info") if _route else None
                        if _movie:
                            _poster_html = f'<img src="{_movie.get("poster_url")}" style="width:64px;border-radius:6px;margin-right:12px;" />' if _movie.get("poster_url") else ""
                            _cast_str = ", ".join(_movie.get("cast") or []) or "—"
                            st.markdown(textwrap.dedent(f'''\
                            <div style="margin-top:10px;padding:12px 14px;border:1px solid #3a3226;border-radius:10px;background:rgba(143,174,100,0.05);display:flex;align-items:flex-start;">
                                {_poster_html}
                                <div>
                                    <div style="font-size:14px;font-weight:800;color:#f6efdc;">{html.escape(str(_movie.get("title") or ""))}
                                        <span style="font-weight:400;color:#a99872;font-size:11px;">{html.escape(str(_movie.get("release_date") or ""))}</span></div>
                                    <div style="font-size:11px;color:#a99872;margin-top:2px;">
                                        ⭐ {_movie.get("vote_average", "—")}/10 · {html.escape(", ".join(_movie.get("genres") or []))}
                                        {f' · {_movie.get("runtime_minutes")} min' if _movie.get("runtime_minutes") else ''}
                                    </div>
                                    <div style="font-size:11px;color:#a99872;margin-top:4px;">Director: {html.escape(str(_movie.get("director") or "—"))}</div>
                                    <div style="font-size:11px;color:#a99872;">Cast: {html.escape(_cast_str)}</div>
                                </div>
                            </div>
                            '''), unsafe_allow_html=True)

                        _breakout = _route.get("breakout_candidates") if _route else None
                        if _breakout and _breakout.get("candidates"):
                            _rows_html = ""
                            for _c in _breakout["candidates"]:
                                _dchg = _c.get("day_change_pct")
                                _dchg_color = "#8fae64" if (_dchg or 0) >= 0 else "#c4685a"
                                _dchg_str = f"{_dchg:+.2f}% today" if _dchg is not None else "day change n/a"
                                _rows_html += (
                                    '<div style="display:flex;justify-content:space-between;align-items:center;'
                                    'padding:8px 0;border-top:1px solid #3a3226;">'
                                    f'<div><span style="font-weight:700;color:#f6efdc;">{html.escape(str(_c.get("symbol") or ""))}</span>'
                                    f' <span style="color:#a99872;font-size:11px;">{html.escape(str(_c.get("name") or ""))}</span></div>'
                                    f'<div style="text-align:right;font-size:12px;">'
                                    f'₹{_c.get("price")} <span style="color:{_dchg_color};">({_dchg_str})</span><br>'
                                    f'<span style="color:#a99872;">5d {_c.get("five_day_change_pct"):+.2f}% · vol +{_c.get("volume_growth_pct"):.0f}%</span>'
                                    '</div></div>'
                                )
                            st.markdown(textwrap.dedent(f'''\
                            <div style="margin-top:10px;padding:12px 14px;border:1px solid #3a3226;border-radius:10px;background:rgba(143,174,100,0.05);">
                                <div style="font-size:12px;font-weight:800;color:#f6efdc;letter-spacing:0.03em;">📈 BREAKOUT SCREEN</div>
                                <div style="font-size:10.5px;color:#8a7d5f;margin-top:2px;">Momentum + volume heuristic from cached data — a shortlist worth a look, not a guarantee.</div>
                                {_rows_html}
                            </div>
                            '''), unsafe_allow_html=True)

                        _trade = _route.get("trade_result") if _route else None
                        if _trade:
                            if _trade.get("ok"):
                                st.markdown(textwrap.dedent(f'''\
                                <div style="margin-top:10px;padding:12px 14px;border:1px solid #3a4a2a;border-radius:10px;background:rgba(143,174,100,0.08);">
                                    <div style="font-size:12px;font-weight:800;color:#8fae64;">✅ ORDER PLACED — {html.escape(str(_trade.get("status") or ""))}</div>
                                    <div style="font-size:11px;color:#c9bd9e;margin-top:4px;">Filled {_trade.get("filled_quantity", 0)} @ ₹{_trade.get("average_price") or "—"} · order id {html.escape(str(_trade.get("broker_order_id") or "—"))}</div>
                                </div>
                                '''), unsafe_allow_html=True)
                            elif _trade.get("insufficient_funds"):
                                st.markdown(textwrap.dedent(f'''\
                                <div style="margin-top:10px;padding:12px 14px;border:1px solid #5a2a2a;border-radius:10px;background:rgba(196,104,90,0.08);">
                                    <div style="font-size:12px;font-weight:800;color:#c4685a;">⚠️ ORDER NOT PLACED — INSUFFICIENT BALANCE</div>
                                    <div style="font-size:11px;color:#c9bd9e;margin-top:4px;">Needs ~₹{_trade.get("required", 0):,.2f} · wallet has ₹{_trade.get("available", 0):,.2f} available.</div>
                                </div>
                                '''), unsafe_allow_html=True)
                            else:
                                st.markdown(textwrap.dedent(f'''\
                                <div style="margin-top:10px;padding:12px 14px;border:1px solid #5a2a2a;border-radius:10px;background:rgba(196,104,90,0.08);">
                                    <div style="font-size:12px;font-weight:800;color:#c4685a;">⚠️ ORDER FAILED</div>
                                    <div style="font-size:11px;color:#c9bd9e;margin-top:4px;">{html.escape(str(_trade.get("error") or "Unknown error"))}</div>
                                </div>
                                '''), unsafe_allow_html=True)
    
                # Optimistic rendering and waiting
                if st.session_state.get("pending_prompt_text") or st.session_state.get("pending_audio_b64"):
                    with st.chat_message("cfa", avatar="🤖"):
                        with st.spinner("Daily Productivity agent is thinking..."):
                            pt = st.session_state.get("pending_prompt_text", "")
                            ab = st.session_state.get("pending_audio_b64")
                            
                            mode_val = "VOICE" if speak_reply else "TEXT"
                            # All messages except the last one (which is the pending query we just appended)
                            recent_history = [
                                {"role": "assistant" if m["role"] == "cfa" else "user", "text": m["text"]}
                                for m in st.session_state.voice_history[:-1][-6:]
                            ]
                            try:
                                _user_identifier = st.session_state.get("fb_email", "anon")
                                
                                if "chat_session_id" not in st.session_state:
                                    import uuid
                                    st.session_state.chat_session_id = uuid.uuid4().hex
                                    
                                _unique_session = f"{_user_identifier}_{st.session_state.chat_session_id}"
                                
                                resp = requests.post(f"{BACKEND_URL}/api/market/voice", json={
                                    "prompt": pt,
                                    "audio_in_base64": ab or "",
                                    "persona": persona,
                                    "session_id": _unique_session,
                                    "mode": mode_val,
                                    "location": st.session_state.user_location,
                                    "history": recent_history,
                                    "id_token": get_id_token(),
                                }, timeout=60)
                                
                                if resp.status_code == 200:
                                    data = resp.json()
                                    # Update transcript if it was voice
                                    if ab and data.get("transcript"):
                                        st.session_state.voice_history[-1]["text"] = data.get("transcript")
                                    
                                    st.session_state.voice_history.append({
                                        "role": "cfa",
                                        "text": data.get("text", "No response received."),
                                        "audio": data.get("audio_base64"),
                                        "route": data.get("route"),
                                    })
                                else:
                                    try:
                                        err_text = resp.json().get("detail", resp.text)
                                    except Exception:
                                        err_text = resp.text
                                    st.session_state.voice_history.append({"role": "cfa", "text": f"**Error {resp.status_code}**: {err_text}"})
                            except Exception as e:
                                st.session_state.voice_history.append({"role": "cfa", "text": f"**Connection failed**: {e}"})
                            finally:
                                st.session_state.pending_prompt_text = None
                                st.session_state.pending_audio_b64 = None
                                st.rerun(scope="fragment")
    
            # ── Text input ──
            user_query = st.chat_input("Ask your Daily Productivity Assistant...", key="bot_chat_input")
            if user_query:
                st.session_state.voice_history.append({"role": "user", "text": user_query})
                st.session_state.pending_prompt_text = user_query
                st.rerun(scope="fragment")
    
            # ── Voice input (mic) ──
            mic_audio = st.audio_input("Or ask by voice", key="cfa_mic_input")
            if mic_audio is not None:
                audio_id = f"{mic_audio.name}-{mic_audio.size}" if hasattr(mic_audio, "size") else str(len(mic_audio.getvalue()))
                if audio_id != st.session_state.get("last_audio_id"):
                    st.session_state.last_audio_id = audio_id
                    audio_bytes = mic_audio.getvalue()
                    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                    st.session_state.voice_history.append({"role": "user", "text": "(voice message)"})
                    st.session_state.pending_audio_b64 = audio_b64
                    st.rerun(scope="fragment")
    render_cfa_panel()