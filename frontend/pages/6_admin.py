import streamlit as st
import requests

from auth_helper import login_widget, auth_headers, get_role, is_admin
from ui_helpers import hide_streamlit_chrome, render_page_nav

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Admin", layout="wide", initial_sidebar_state="collapsed")
hide_streamlit_chrome()

if not login_widget(BACKEND_URL):
    st.stop()

render_page_nav()

st.title("🛡️ Admin Dashboard")

# Role is re-checked server-side on every /api/admin/* call regardless of
# what's shown here — this client-side check only controls whether the
# page bothers to render the table, it is not the security boundary.
if not is_admin():
    st.warning(
        "This page is only visible to admin accounts. If you believe this is a mistake, "
        "ask an existing admin to promote your account, or add your email to the "
        "ADMIN_BOOTSTRAP_EMAILS environment variable and log out/in again "
        "(only takes effect on first-ever login, not a re-login after that)."
    )
    st.stop()

st.caption(
    "Lists every account that has ever signed in — email, role, and when they first/last "
    "logged in. Pulled from each user's own profile doc only — never from their journals, "
    "trades, or broker configuration, which stay private to that user."
)

# ── Feature columns — must match backend/app/routers/auth.py's DEFAULT_FEATURES ──
FEATURE_COLUMNS = [
    ("daily_productivity", "Daily Productivity Agent", "On by default for every user."),
    ("news", "News", "Global, National and Local headlines."),
    ("entertainment", "Entertainment", "Movie news & OTT releases."),
    ("equity_news", "Equity News", "Live Market Sentiment + Live Wire."),
    ("smart_investor", "Smart Investor", "Equity Side — screener, mutual funds, dividends, results, trade terminal."),
]

if st.button("↺ Refresh"):
    st.cache_data.clear()


@st.cache_data(ttl=30)
def fetch_users():
    try:
        r = requests.get(f"{BACKEND_URL}/api/admin/users", headers=auth_headers(), timeout=20)
        if r.status_code == 200:
            return r.json(), None
        return None, f"{r.status_code}: {r.text}"
    except Exception as e:
        return None, str(e)


def save_features(uid: str, features: dict):
    try:
        r = requests.post(
            f"{BACKEND_URL}/api/admin/users/{uid}/features",
            json=features, headers=auth_headers(), timeout=15,
        )
        if r.status_code == 200:
            st.cache_data.clear()
            st.success("Access updated.")
            st.rerun()
        else:
            st.error(f"Couldn't save: {r.status_code}: {r.text}")
    except Exception as e:
        st.error(f"Couldn't save: {e}")


data, error = fetch_users()

if error:
    st.error(f"Couldn't load users: {error}")
elif data:
    users = data.get("users", [])
    c1, c2 = st.columns(2)
    c1.metric("Total users", data.get("total", len(users)))
    c2.metric("Admins", sum(1 for u in users if u.get("role") == "admin"))

    st.divider()
    st.subheader("Per-user home-page access")
    st.caption(
        "Decide what each user's home page shows. Daily Productivity Agent is on by default for "
        "everyone; tick the rest of a row to give that user those sections, then hit Save."
    )

    if not users:
        st.info("No users provisioned yet.")
    else:
        header = st.columns([2.4] + [1] * len(FEATURE_COLUMNS) + [0.9])
        header[0].markdown("**User**")
        for i, (_, label, help_text) in enumerate(FEATURE_COLUMNS):
            header[i + 1].markdown(f"**{label}**", help=help_text)
        header[-1].markdown("**Save**")

        for u in users:
            uid = u.get("uid")
            email = u.get("email") or uid
            role = u.get("role", "user")
            current = u.get("features") or {}

            row = st.columns([2.4] + [1] * len(FEATURE_COLUMNS) + [0.9])
            row[0].markdown(f"{email}" + (" · *admin*" if role == "admin" else ""))

            new_values = {}
            for i, (key, label, help_text) in enumerate(FEATURE_COLUMNS):
                new_values[key] = row[i + 1].checkbox(
                    "", value=bool(current.get(key, key == "daily_productivity")),
                    key=f"feat_{uid}_{key}", label_visibility="collapsed",
                )

            if row[-1].button("💾", key=f"save_{uid}", help="Save access for this user"):
                save_features(uid, new_values)
else:
    st.info("No users provisioned yet.")