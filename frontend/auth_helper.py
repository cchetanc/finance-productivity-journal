"""
Minimal Firebase email/password auth for the Streamlit frontend.

Uses the Firebase Identity Toolkit REST API directly (no firebase-admin
needed client-side — that's a server SDK). Requires:
  1. Email/Password sign-in enabled in Firebase Console -> Authentication -> Sign-in method.
  2. config/firebase_config.json's "apiKey" set to your project's real
     Web API key (Firebase Console -> Project settings -> General ->
     Web API Key). It's a public identifier, safe to ship client-side —
     it is NOT a secret — but it must be the real value, not the placeholder.
  3. At least one user created (Firebase Console -> Authentication -> Users
     -> Add user), since this is sign-IN, not self-serve sign-up.

The resulting idToken is what the backend's verify_id_token() checks on
every /api/trading/* call. Firebase ID tokens expire after 1 hour; this
module refreshes automatically using the refreshToken when a call comes
back 401.
"""
import json
import os
import time

import requests
import streamlit as st

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "firebase_config.json")


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
    return bool(st.session_state.get("fb_id_token"))


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
    for k in ("fb_id_token", "fb_refresh_token", "fb_token_expiry", "fb_email"):
        st.session_state.pop(k, None)


def login_widget() -> bool:
    """Renders a login form if not already signed in. Returns True once
    the user is authenticated (call this at the top of any gated page)."""
    if is_logged_in():
        return True

    if not _load_firebase_api_key():
        st.error(
            "Firebase isn't configured yet: frontend/firebase_config.json still has a "
            "placeholder apiKey. Set it to your project's real Web API Key "
            "(Firebase Console -> Project settings -> General) before this page can work."
        )
        return False

    st.subheader("Sign in")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
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
    return False