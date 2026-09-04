"""
Firebase email/password auth for the Streamlit frontend — sign-in AND
self-serve sign-up, both via the Firebase Identity Toolkit REST API (no
firebase-admin needed client-side — that's a server SDK, and no manual
"add user" step in the Firebase Console is needed either).

Requires:
  1. Email/Password sign-in enabled in Firebase Console -> Authentication ->
     Sign-in method (this is a one-time provider toggle, not a per-user step).
  2. config/firebase_config.json's "apiKey" set to your project's real
     Web API key. It's a public identifier, safe to ship client-side — it
     is NOT a secret — but it must be the real value, not the placeholder.

Account policy (enforced here, client-side, before any request reaches
Firebase):
  - Email must end in @gmail.com. NOTE: this is an *app-level* gate, not an
    identity-level one — Firebase's REST signUp endpoint itself will accept
    any address, so a request sent straight to Firebase's API (bypassing
    this UI) could still create a non-Gmail Firebase Auth account. The
    backend's /api/auth/provision step re-checks the domain and simply
    refuses to create an app profile for anything else, so such an account
    would authenticate but get no access to the app. To close that gap at
    the identity layer itself (reject the signup before it's ever created),
    add a Firebase Auth "Blocking Function" (beforeCreate) — flagged as a
    follow-up, not done here.
  - Password: at least 8 characters, at least one letter, one digit, and
    one special character. Firebase's own default minimum is just 6 chars
    with no complexity rule, so this stricter policy is enforced here, not
    by Firebase.

The resulting idToken is what the backend's verify_id_token() checks on
every authenticated call. Firebase ID tokens expire after 1 hour; this
module refreshes automatically using the refreshToken when needed.
"""
import json
import os
import re
import time

import requests
import streamlit as st

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "firebase_config.json")
ALLOWED_EMAIL_DOMAIN = "gmail.com"
_SPECIAL_CHARS = r"""!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?"""

# Mirrors backend/app/routers/auth.py's DEFAULT_FEATURES — used only as a
# client-side fallback before /api/auth/provision has returned (or if it
# fails), so the home page still renders something sane rather than crash.
DEFAULT_FEATURES = {
    "daily_productivity": True,
    "news": False,
    "entertainment": False,
    "equity_news": False,
    "smart_investor": False,
}


@st.cache_data(ttl=3600)
def _load_firebase_api_key() -> str | None:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        key = cfg.get("apiKey", "")
        if not key or key.startswith("YOUR_"):
            return None
        return key
    except Exception:
        return None


def validate_email_domain(email: str) -> str | None:
    """Returns an error message, or None if the email is an @gmail.com address."""
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return "Enter a valid email address."
    if not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        return f"Only @{ALLOWED_EMAIL_DOMAIN} addresses can sign up."
    return None


def validate_password(password: str) -> str | None:
    """Returns an error message, or None if the password meets policy:
    at least 8 characters, at least one letter, one digit, one special
    character."""
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password):
        return "Password must include at least one letter."
    if not re.search(r"[0-9]", password):
        return "Password must include at least one digit."
    if not re.search(f"[{_SPECIAL_CHARS}]", password):
        return "Password must include at least one special character (e.g. ! @ # $ %)."
    return None


def _sign_in(email: str, password: str) -> tuple[dict | None, str | None]:
    api_key = _load_firebase_api_key()
    if not api_key:
        return None, "API Key missing"
    resp = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    if resp.status_code != 200:
        return None, resp.text
    return resp.json(), None


def _sign_up(email: str, password: str) -> tuple[dict | None, str | None]:
    """Creates a brand-new Firebase Auth user directly via the REST API —
    no Firebase Console / firebase-admin step needed. Anyone can call this
    from the app itself; the @gmail.com + password-policy checks above run
    first so Firebase only ever sees requests that already pass policy."""
    api_key = _load_firebase_api_key()
    if not api_key:
        return None, "API Key missing"
    resp = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}",
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    if resp.status_code != 200:
        return None, resp.text
    return resp.json(), None


def _refresh(refresh_token: str) -> dict | None:
    api_key = _load_firebase_api_key()
    if not api_key:
        return None
    resp = requests.post(
        f"https://securetoken.googleapis.com/v1/token?key={api_key}",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return {
        "idToken": data["id_token"],
        "refreshToken": data["refresh_token"],
        "expiresIn": data["expires_in"],
    }


def is_logged_in() -> bool:
    return bool(get_id_token())


def get_id_token() -> str | None:
    """Returns a valid ID token, transparently refreshing it if it's
    expired or about to expire. Returns None if not logged in."""
    token = st.session_state.get("fb_id_token")
    if not token:
        return None
    if time.time() >= st.session_state.get("fb_token_expiry", 0):
        refreshed = _refresh(st.session_state["fb_refresh_token"])
        if not refreshed:
            logout()
            return None
        st.session_state["fb_id_token"] = refreshed["idToken"]
        st.session_state["fb_refresh_token"] = refreshed["refreshToken"]
        st.session_state["fb_token_expiry"] = time.time() + int(refreshed["expiresIn"]) - 60
        token = st.session_state["fb_id_token"]
    return token


def auth_headers() -> dict:
    token = get_id_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def logout():
    for k in ("fb_id_token", "fb_refresh_token", "fb_token_expiry", "fb_email",
              "fb_role", "fb_provisioned", "fb_features", "voice_history",
              "chat_loaded_date", "cfa_persona"):
        st.session_state.pop(k, None)


def _provision(backend_url: str):
    """Calls the backend right after a successful sign-in/sign-up to
    create-or-touch this user's Firestore profile doc and learn their role
    (user/admin). Cached in session_state so it only runs once per browser
    session, not on every page."""
    if st.session_state.get("fb_provisioned"):
        return
    try:
        resp = requests.post(f"{backend_url}/api/auth/provision", headers=auth_headers(), timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            st.session_state["fb_role"] = data.get("role", "user")
            st.session_state["fb_features"] = {**DEFAULT_FEATURES, **(data.get("features") or {})}
            st.session_state["fb_provisioned"] = True
        else:
            # Provisioning failure (e.g. non-Gmail account created some
            # other way) shouldn't crash the page — just leave the role as
            # "user" and let backend-side authorization checks handle it.
            st.session_state["fb_role"] = "user"
            st.session_state["fb_features"] = dict(DEFAULT_FEATURES)
    except Exception:
        st.session_state["fb_role"] = "user"
        st.session_state["fb_features"] = dict(DEFAULT_FEATURES)


def get_role() -> str:
    return st.session_state.get("fb_role", "user")


def is_admin() -> bool:
    return get_role() == "admin"


def get_features() -> dict:
    """Which home-page sections this user is allowed to see — set by an
    admin from the Admin page (backend/app/routers/admin.py's
    set_user_features). Falls back to DEFAULT_FEATURES if provisioning
    hasn't completed yet.

    Admin accounts always get every section on, regardless of whatever is
    stored in their own profile doc — the whole point of the role is to
    configure what OTHERS see, and an admin whose own profile doc predates
    the RBAC feature (or was never explicitly turned on) shouldn't end up
    staring at a blank dashboard. Non-admin users still follow exactly
    what's stored server-side (see /api/admin/users/{uid}/features)."""
    if get_role() == "admin":
        return {k: True for k in DEFAULT_FEATURES}
    return st.session_state.get("fb_features") or dict(DEFAULT_FEATURES)


def login_widget(backend_url: str = "") -> bool:
    """Renders a sign-in / create-account form if not already signed in.
    Returns True once the user is authenticated (call this at the top of
    every page — this is the app-wide gate, not just the trade terminal).
    Pass backend_url so the first successful auth can provision the user's
    Firestore profile/role."""
    if is_logged_in():
        if backend_url:
            _provision(backend_url)
        return True

    if not _load_firebase_api_key():
        st.error(
            "Firebase isn't configured yet: frontend/firebase_config.json still has a "
            "placeholder apiKey. Set it to your project's real Web API Key "
            "(Firebase Console -> Project settings -> General) before this page can work."
        )
        return False

    st.subheader("Sign in")
    tab_in, tab_up = st.tabs(["Sign in", "Create account"])

    with tab_in:
        with st.form("login_form"):
            email = st.text_input("Email", key="li_email")
            password = st.text_input("Password", type="password", key="li_password")
            submitted = st.form_submit_button("Sign in")
            if submitted:
                data, error_msg = _sign_in(email, password)
                if data:
                    st.session_state["fb_id_token"] = data["idToken"]
                    st.session_state["fb_refresh_token"] = data["refreshToken"]
                    st.session_state["fb_token_expiry"] = time.time() + int(data["expiresIn"]) - 60
                    st.session_state["fb_email"] = email
                    st.rerun()
                else:
                    st.error(f"Sign-in failed. Error from Firebase: {error_msg}")

    with tab_up:
        st.caption(f"Only @{ALLOWED_EMAIL_DOMAIN} addresses can sign up. "
                   "Password: 8+ characters, with a letter, a digit, and a special character.")
        with st.form("signup_form"):
            email = st.text_input("Gmail address", key="su_email")
            password = st.text_input("Create a password", type="password", key="su_password")
            confirm = st.text_input("Confirm password", type="password", key="su_confirm")
            submitted = st.form_submit_button("Create account")
            if submitted:
                domain_err = validate_email_domain(email)
                pw_err = validate_password(password)
                if domain_err:
                    st.error(domain_err)
                elif pw_err:
                    st.error(pw_err)
                elif password != confirm:
                    st.error("Passwords don't match.")
                else:
                    data, error_msg = _sign_up(email, password)
                    if data:
                        st.session_state["fb_id_token"] = data["idToken"]
                        st.session_state["fb_refresh_token"] = data["refreshToken"]
                        st.session_state["fb_token_expiry"] = time.time() + int(data["expiresIn"]) - 60
                        st.session_state["fb_email"] = email
                        st.rerun()
                    elif error_msg and "EMAIL_EXISTS" in error_msg:
                        st.error("An account with this email already exists — use the Sign in tab.")
                    else:
                        st.error(f"Sign-up failed. Error from Firebase: {error_msg}")
    return False