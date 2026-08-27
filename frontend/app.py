import streamlit as st
import streamlit.components.v1 as components
import requests

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Finance Intelligence Journal", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    background-color: #0b0e14 !important;
    color: #c9d1d9 !important;
}
header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { padding: 18px 26px !important; max-width: 100% !important; }
[data-testid="stButton"] > button {
    background: transparent !important;
    border: 1px solid #30363d !important;
    color: #484f58 !important;
    font-weight: 600 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 10px !important;
    letter-spacing: 0.5px !important;
    padding: 3px 8px !important;
    border-radius: 4px !important;
    transition: all 0.15s ease !important;
    line-height: 1.4 !important;
    min-height: 0 !important;
    height: auto !important;
}
[data-testid="stButton"] > button:hover {
    border-color: #3fb950 !important;
    color: #3fb950 !important;
}
iframe { display: block; }
</style>
""", unsafe_allow_html=True)

# ── SESSION STATE ──────────────────────────────────────────────────────────────
for key in ("v_mood", "v_news"):
    if key not in st.session_state:
        st.session_state[key] = 0

# ── HEADER ────────────────────────────────────────────────────────────────────
components.html("""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@700;900&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 6px #3fb950;}50%{opacity:0.4;box-shadow:0 0 2px #3fb950;}}</style>
<div style="background:#0d1117;border-bottom:1px solid #21262d;padding:12px 26px;display:flex;align-items:center;justify-content:space-between;margin:-18px -26px 14px -26px;">
    <div style="display:flex;align-items:center;gap:12px;">
        <div style="width:7px;height:7px;background:#3fb950;border-radius:50%;animation:pulse 2s infinite;flex-shrink:0;"></div>
        <span style="font-family:'Inter',sans-serif;font-size:12px;font-weight:900;letter-spacing:2.5px;color:#e6edf3;text-transform:uppercase;">Finance Intelligence Journal</span>
    </div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#484f58;letter-spacing:1px;">IDEATHON III &nbsp;·&nbsp; GEMINI NATIVE &nbsp;·&nbsp; NSE LIVE</div>
</div>
""", height=54)

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

# ── LOAD DATA ──────────────────────────────────────────────────────────────────
mood_data    = fetch_mood(st.session_state.v_mood)
score        = mood_data.get("systemic_score", 50)
bias         = mood_data.get("bias", "NEUTRAL")
flags        = mood_data.get("macro_risk_flags", [])
indices      = mood_data.get("indices", [])
index_groups = mood_data.get("index_groups", {})
sc           = "#f85149" if score < 35 else ("#e3b341" if score < 65 else "#3fb950")

news_items   = fetch_news_raw(st.session_state.v_news)
sent_map     = {}
if news_items:
    titles_t = tuple(n["title"] for n in news_items)
    with st.spinner("FinBERT scoring market sentiment..."):
        sent_map = fetch_sentiments(st.session_state.v_news, titles_t)
for n in news_items:
    n["sentiment"] = sent_map.get(n["title"], "NEUTRAL")

# ── ROW 1: SENTIMENT PANEL + LIVE WIRE ────────────────────────────────────────
col1, col2 = st.columns([1, 1.8])

with col1:
    hdr, btn = st.columns([6, 1])
    with hdr:
        st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#8b949e;text-transform:uppercase;margin:0 0 5px 0;">◎ Live Market Sentiment</p>', unsafe_allow_html=True)
    with btn:
        if st.button("↺", key="ref_mood"):
            st.session_state.v_mood += 1
            st.cache_data.clear()
            st.rerun()

    flags_html = "".join([
        f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:8px;">'
        f'<div style="width:4px;height:4px;background:{sc};border-radius:50%;flex-shrink:0;"></div>'
        f'<span style="font-size:11.5px;color:#c9d1d9;font-weight:500;">{f}</span></div>'
        for f in (flags or ["No risk flags"])
    ])
    components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:0.3;}}}}</style>
<div style="background:#151921;border:1px solid #21262d;border-radius:8px;padding:20px 22px;height:258px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #21262d;padding-bottom:15px;margin-bottom:14px;">
        <div>
            <div style="font-size:10px;color:#8b949e;text-transform:uppercase;font-weight:600;letter-spacing:1px;margin-bottom:5px;">Quantitative Bias</div>
            <div style="font-size:26px;font-weight:900;color:{sc};letter-spacing:0.5px;">{bias}</div>
        </div>
        <div style="text-align:right;">
            <div style="font-size:10px;color:#8b949e;text-transform:uppercase;font-weight:600;letter-spacing:1px;margin-bottom:5px;">Macro Score</div>
            <div><span style="font-size:44px;font-weight:900;color:{sc};line-height:1;">{score}</span><span style="font-size:16px;color:#8b949e;font-weight:600;"> / 100</span></div>
        </div>
    </div>
    <div style="font-size:10px;color:#8b949e;text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:10px;">Live Risk Flags</div>
    {flags_html}
</div>
""", height=276)

with col2:
    hdr, btn = st.columns([6, 1])
    with hdr:
        st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#8b949e;text-transform:uppercase;margin:0 0 5px 0;">((•)) Live Wire — FinBERT Scored</p>', unsafe_allow_html=True)
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
        link  = n.get("link", "#")
        title = n.get("title", "").replace('"', '&quot;').replace("'", "&#39;")
        src   = n.get("source", "")[:20]
        pub   = n.get("published", "")

        if sent == "BULLISH":
            sym, s_col, s_bg = "▲", "#3fb950", "rgba(63,185,80,0.08)"
        elif sent == "BEARISH":
            sym, s_col, s_bg = "▼", "#f85149", "rgba(248,81,73,0.08)"
        else:
            sym, s_col, s_bg = "●", "#8b949e", "transparent"

        rows_html += f"""
<div style="padding:8px 12px 8px 13px;margin-bottom:3px;border-left:3px solid {s_col};background:{s_bg};">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
        <span style="font-size:14px;color:{s_col};font-weight:900;line-height:1;">{sym}</span>
        <span style="font-size:9px;font-weight:700;color:{s_col};font-family:monospace;border:1px solid {s_col};padding:1px 5px;border-radius:2px;">{sent}</span>
        <span style="font-size:9px;color:#484f58;font-family:monospace;">{src} · {pub}</span>
    </div>
    <a href="{link}" target="_blank"
       style="font-size:12.5px;color:#c9d1d9;text-decoration:none;line-height:1.5;font-weight:500;display:block;"
       onmouseover="this.style.color='#58a6ff'" onmouseout="this.style.color='#c9d1d9'">{title}</a>
</div>"""

    if not rows_html:
        rows_html = '<div style="padding:20px 0;color:#484f58;font-size:13px;">No live news. Try ↺ refresh.</div>'

    components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:0.35;}}}}
@keyframes scrollUp{{0%{{transform:translateY(0);}}100%{{transform:translateY(-33.333%);}}}}
.wbox{{background:#151921;border:1px solid #21262d;border-radius:8px;padding:14px 16px;height:258px;overflow:hidden;position:relative;}}
.wbox::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:55px;background:linear-gradient(to top,#151921 40%,transparent);z-index:1;pointer-events:none;border-radius:0 0 8px 8px;}}
.wtrack{{animation:scrollUp {anim_dur}s linear infinite;will-change:transform;}}
</style>
<div class="wbox">
    <div class="wtrack">{rows_html}</div>
</div>
""", height=276)

# ── ROW 2: SECTOR HEAT MAP (dynamic groups from backend) ─────────────────────
st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

hdr, btn = st.columns([8, 1])
with hdr:
    st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:1.5px;color:#8b949e;text-transform:uppercase;margin:0 0 5px 0;">⚡ Sector Heat Map & Global Indices</p>', unsafe_allow_html=True)
with btn:
    if st.button("↺", key="ref_heat"):
        st.session_state.v_mood += 1
        st.cache_data.clear()
        st.rerun()

# Build index lookup
idx_map = {i["name"]: i for i in indices}

# Fall back to flat layout if no groups returned
if not index_groups:
    index_groups = {"All Indices": [i["name"] for i in indices]}

tiles_html = ""
for group_name, names in index_groups.items():
    group_tiles = ""
    for name in names:
        idx = idx_map.get(name)
        if not idx or idx.get("price", 0) == 0:
            group_tiles += f'<div style="background:#111722;border:1px solid #1c2128;border-radius:5px;padding:12px 14px;"><div style="font-size:10px;color:#484f58;font-weight:600;margin-bottom:6px;">{name}</div><div style="font-size:13px;color:#30363d;font-family:monospace;">N/A</div></div>'
            continue

        price    = f"{idx['price']:,.2f}"
        chg      = idx.get("change", 0)
        pct      = idx.get("change_pct", 0)
        pos      = idx.get("positive", False)
        arrow    = "▲" if pos else "▼"
        c_col    = "#3fb950" if pos else "#f85149"
        c_bg     = "#0e1f14" if pos else "#1e0e0e"
        c_bdr    = "#1a3d25" if pos else "#3d1515"
        chg_str  = f"{chg:+.2f}"
        pct_str  = f"({pct:+.2f}%)"

        group_tiles += f"""
<div style="background:{c_bg};border:1px solid {c_bdr};border-radius:5px;padding:12px 14px;min-width:0;">
    <div style="font-size:10px;color:#8b949e;font-weight:600;margin-bottom:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{name}</div>
    <div style="font-size:15px;font-weight:700;color:#e6edf3;font-family:'JetBrains Mono',monospace;margin-bottom:4px;white-space:nowrap;">{price}</div>
    <div style="font-size:10.5px;color:{c_col};font-family:'JetBrains Mono',monospace;white-space:nowrap;">{arrow} {chg_str} {pct_str}</div>
</div>"""

    tiles_html += f"""
<div style="margin-bottom:14px;">
    <div style="font-size:9.5px;color:#484f58;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding-bottom:6px;margin-bottom:8px;border-bottom:1px solid #1c2128;">{group_name}</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">{group_tiles}</div>
</div>"""

if not tiles_html:
    tiles_html = '<div style="color:#484f58;font-size:13px;padding:20px 0;">Fetching live data... Try ↺ refresh.</div>'

components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<div style="background:#151921;border:1px solid #21262d;border-radius:8px;padding:16px 20px;font-family:'Inter',sans-serif;">
    {tiles_html}
    <div style="font-size:10px;color:#3d4451;margin-top:8px;font-style:italic;">Via Yahoo Finance · ~15 min delay · NSE · BSE · NASDAQ · S&amp;P · DJI</div>
</div>
""", height=520)
