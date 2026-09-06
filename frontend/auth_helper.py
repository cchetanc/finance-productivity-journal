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
import extra_streamlit_components as stx

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "firebase_config.json")
ALLOWED_EMAIL_DOMAIN = "gmail.com"
_SPECIAL_CHARS = r"""!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?"""

# Firebase Identity Toolkit returns machine-readable codes (e.g.
# "INVALID_LOGIN_CREDENTIALS") inside a {"error": {...}} JSON body on
# failure. Left unhandled, that raw JSON blob — nested objects, an
# "errors" array, numeric codes and all — used to get dumped straight
# onto the sign-in page as the error message. This maps the codes we're
# likely to see to plain English instead.
_FRIENDLY_ERRORS = {
    "EMAIL_NOT_FOUND": "No account found with that email.",
    "INVALID_PASSWORD": "Incorrect password.",
    "INVALID_LOGIN_CREDENTIALS": "Incorrect email or password.",
    "USER_DISABLED": "This account has been disabled.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many failed attempts. Please wait a bit and try again.",
    "EMAIL_EXISTS": "An account with this email already exists — use the Sign in tab.",
    "OPERATION_NOT_ALLOWED": "Email/password sign-in isn't enabled for this project.",
    "INVALID_EMAIL": "That doesn't look like a valid email address.",
    "WEAK_PASSWORD": "Password is too weak.",
    "CREDENTIAL_TOO_OLD_LOGIN_AGAIN": "Your session is too old — please sign in again.",
}


def _describe_error(error_msg: str) -> str:
    """Turn whatever _sign_in/_sign_up/_refresh returned as an error into
    one short, human-readable line. `error_msg` is either a plain-English
    string we wrote ourselves (missing API key, network failure) or the
    raw JSON body Firebase's REST API sent back — in which case we pull
    out just the error code and translate it."""
    try:
        payload = json.loads(error_msg)
        code = payload.get("error", {}).get("message", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return error_msg  # already a plain-English message
    return _FRIENDLY_ERRORS.get(code, f"Sign-in failed ({code or 'unknown error'}).")

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

from streamlit.runtime.scriptrunner import get_script_run_ctx

def get_cookie_manager():
    ctx = get_script_run_ctx()
    if not hasattr(ctx, "cookie_manager"):
        ctx.cookie_manager = stx.CookieManager(key="auth_cookies")
    return ctx.cookie_manager

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
    try:
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=15,
        )
    except requests.RequestException:
        return None, "Couldn't reach the sign-in service. Check your connection and try again."
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
    try:
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}",
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=15,
        )
    except requests.RequestException:
        return None, "Couldn't reach the sign-up service. Check your connection and try again."
    if resp.status_code != 200:
        return None, resp.text
    return resp.json(), None


def _refresh(refresh_token: str) -> dict | None:
    api_key = _load_firebase_api_key()
    if not api_key:
        return None
    try:
        resp = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={api_key}",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
    except requests.RequestException:
        return None
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
    # logout() just ran in this session: don't fall back to reading cookies
    # below, since CookieManager's delete() is applied via a browser-side
    # component round-trip that hasn't necessarily completed yet — reading
    # cm.get() right after can still return the stale, not-yet-deleted
    # cookie value and silently "restore" the session that was just signed
    # out of. This flag is cleared as soon as a fresh sign-in/sign-up sets
    # fb_id_token directly (see login_widget below).
    if st.session_state.get("_signed_out"):
        return None

    cm = get_cookie_manager()
    token = st.session_state.get("fb_id_token")
    
    if not token:
        # Try to restore from cookies on hard refresh
        cookie_token = cm.get("fb_id_token")
        if cookie_token:
            st.session_state["fb_id_token"] = cookie_token
            st.session_state["fb_refresh_token"] = cm.get("fb_refresh_token")
            st.session_state["fb_token_expiry"] = float(cm.get("fb_token_expiry") or 0)
            st.session_state["fb_email"] = cm.get("fb_email")
            st.session_state["fb_role"] = cm.get("fb_role") or "user"
            
            # Since features is a dict, we might need to eval it if it was stringified, or just skip it and let it re-provision
            token = cookie_token
            
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
        
        # Update cookies with new tokens
        cm.set("fb_id_token", st.session_state["fb_id_token"], key="set_id_token_ref")
        cm.set("fb_refresh_token", st.session_state["fb_refresh_token"], key="set_refresh_token_ref")
        cm.set("fb_token_expiry", str(st.session_state["fb_token_expiry"]), key="set_token_expiry_ref")
        
    return token


def auth_headers() -> dict:
    token = get_id_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


# Only these five are ever actually written to a browser cookie (see the
# cm.set(...) calls in login_widget() and get_id_token() below) — the rest
# are plain session_state bookkeeping with no cookie counterpart.
_COOKIE_KEYS = ("fb_id_token", "fb_refresh_token", "fb_token_expiry", "fb_email", "fb_role")
_SESSION_ONLY_KEYS = ("fb_provisioned", "fb_features", "voice_history", "chat_loaded_date", "cfa_persona")


def logout():
    """Requests sign-out. The actual work happens in
    _finish_logout_if_requested(), called from login_widget() on the very
    next run — see that function's docstring for why this is split in two
    steps instead of doing it all here immediately."""
    st.session_state["_logout_requested"] = True


def _finish_logout_if_requested():
    """Step 1 (first run after logout()): issue the cookie deletions and
    rerun once more, while the app still looks/behaves as signed-in (we
    haven't touched session_state yet). Step 2 (the run after that): the
    delete calls have now had a full, uninterrupted render to complete, so
    it's safe to actually clear session_state and flip to signed-out.

    This two-step handoff exists because CookieManager.delete() runs
    through a Streamlit custom component — an async, iframe-based round
    trip. Deleting the cookies and immediately st.rerun()-ing into a
    completely different page (as this used to do in one step) could
    swap that page in before the delete iframes finished, so a stale
    fb_id_token cookie occasionally survived and silently restored the
    old session on the next load — which is what made Sign out feel like
    it needed several clicks to actually take. Call this once, near the
    top of every page (login_widget() already does), before anything else
    reads is_logged_in()."""
    if not st.session_state.get("_logout_requested"):
        return

    if not st.session_state.get("_logout_cookies_cleared"):
        cm = get_cookie_manager()
        for k in _COOKIE_KEYS:
            try:
                cm.delete(k, key=f"del_{k}")
            except KeyError:
                # CookieManager raises if the cookie was never set client-side
                # (e.g. never restored this session) — deletion is a no-op then.
                pass
        st.session_state["_logout_cookies_cleared"] = True
        st.rerun()

    for k in (*_COOKIE_KEYS, *_SESSION_ONLY_KEYS):
        st.session_state.pop(k, None)
    st.session_state.pop("_logout_requested", None)
    st.session_state.pop("_logout_cookies_cleared", None)
    # See get_id_token()'s comment: this makes sign-out take effect
    # immediately regardless of whether the cookie deletions have finished
    # propagating to the browser yet.
    st.session_state["_signed_out"] = True
    st.rerun()


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
    _finish_logout_if_requested()

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
                    st.session_state.pop("_signed_out", None)
                    st.session_state["fb_id_token"] = data["idToken"]
                    st.session_state["fb_refresh_token"] = data["refreshToken"]
                    st.session_state["fb_token_expiry"] = time.time() + int(data["expiresIn"]) - 60
                    st.session_state["fb_email"] = email
                    
                    cm = get_cookie_manager()
                    cm.set("fb_id_token", st.session_state["fb_id_token"], key="set_id_token_li")
                    cm.set("fb_refresh_token", st.session_state["fb_refresh_token"], key="set_refresh_token_li")
                    cm.set("fb_token_expiry", str(st.session_state["fb_token_expiry"]), key="set_token_expiry_li")
                    cm.set("fb_email", email, key="set_email_li")
                    
                    st.rerun()
                else:
                    st.error(_describe_error(error_msg))

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
                        st.session_state.pop("_signed_out", None)
                        st.session_state["fb_id_token"] = data["idToken"]
                        st.session_state["fb_refresh_token"] = data["refreshToken"]
                        st.session_state["fb_token_expiry"] = time.time() + int(data["expiresIn"]) - 60
                        st.session_state["fb_email"] = email
                        
                        cm = get_cookie_manager()
                        cm.set("fb_id_token", st.session_state["fb_id_token"], key="set_id_token_su")
                        cm.set("fb_refresh_token", st.session_state["fb_refresh_token"], key="set_refresh_token_su")
                        cm.set("fb_token_expiry", str(st.session_state["fb_token_expiry"]), key="set_token_expiry_su")
                        cm.set("fb_email", email, key="set_email_su")
                        
                        st.rerun()
                    else:
                        st.error(_describe_error(error_msg))
    return False