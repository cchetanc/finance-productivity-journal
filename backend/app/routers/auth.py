"""
User provisioning — the server-side half of account creation.

Firebase Auth itself just proves "this token belongs to a real,
password-verified account." It does NOT know or enforce anything about our
app's own policy (Gmail-only, roles, etc.) — that's what this router is for.
It's called once per browser session, immediately after a successful
sign-in/sign-up (see frontend/auth_helper.py's login_widget()).

Firestore layout:
    /users/{uid}/profile/info = {email, role, created_at, last_login}

This sits under the same /users/{uid}/** subtree that config/firestore.rules
already restricts to "only that uid can read/write it" — so this doc is
exactly as isolated as journals/trades/broker-config already are, with no
rules changes needed.
"""
import datetime
import logging
import os

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..database import db

logger = logging.getLogger("auth.router")
router = APIRouter(prefix="/api/auth", tags=["Auth"])

ALLOWED_EMAIL_DOMAIN = "gmail.com"

# Home-page RBAC — which dashboard sections a given user is allowed to see.
# "daily_productivity" is on by default for every new user; everything else
# starts off and is switched on per-user by an admin from the Admin page
# (see routers/admin.py's set_user_features). Any user profile doc written
# before this feature existed is missing the key entirely — callers should
# merge over DEFAULT_FEATURES (see _with_feature_defaults) rather than
# assume the key is present.
DEFAULT_FEATURES = {
    "daily_productivity": True,   # Daily Productivity Agent (chat)
    "news": False,                # Global / National / Local headlines
    "entertainment": False,       # Movie news & OTT releases
    "equity_news": False,         # Live Market Sentiment + Live Wire
    "smart_investor": False,      # Equity Side (screener, MF, dividends, results, trade terminal)
}


def _with_feature_defaults(features: dict | None) -> dict:
    merged = dict(DEFAULT_FEATURES)
    merged.update(features or {})
    return merged


def _profile_ref(uid: str):
    return db.collection("users").document(uid).collection("profile").document("info")


def _admin_bootstrap_emails() -> set:
    """Comma-separated allowlist of emails that get role='admin' the very
    first time they log in, e.g.:
        ADMIN_BOOTSTRAP_EMAILS=you@gmail.com,cofounder@gmail.com
    Set as a Cloud Run env var — no Firebase Console click-through needed.
    Only consulted at profile-CREATION time (see provision() below), never
    on a later re-login, so it can't be used to silently re-escalate an
    account an admin has since demoted back to 'user' in Firestore."""
    raw = os.environ.get("ADMIN_BOOTSTRAP_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


@router.post("/provision")
def provision(user: dict = Depends(get_current_user)):
    """
    First login  -> creates the profile doc, assigns role (admin if the
                    email is in ADMIN_BOOTSTRAP_EMAILS, else user).
    Later logins -> only touches last_login; role is never changed here,
                    so a later edit to ADMIN_BOOTSTRAP_EMAILS (or manual
                    role change) can't be overwritten by a routine re-login.

    Also re-enforces the @gmail.com policy server-side: the frontend
    already blocks non-Gmail sign-up in the UI, but that's a client-side
    check only — Firebase's own signUp REST endpoint doesn't care what
    domain you use. If a non-Gmail account reaches this endpoint anyway
    (e.g. someone calling Firebase's API directly, bypassing our UI), it
    gets a 403 and never receives an app profile, so it has a valid
    Firebase Auth login but zero access to anything in this app.
    """
    uid, email = user["uid"], (user.get("email") or "")
    if not email.lower().endswith("@" + ALLOWED_EMAIL_DOMAIN):
        raise HTTPException(
            status_code=403,
            detail=f"Only @{ALLOWED_EMAIL_DOMAIN} accounts are permitted in this app.",
        )

    ref = _profile_ref(uid)
    doc = ref.get()
    now = datetime.datetime.utcnow().isoformat()

    if doc.exists:
        data = doc.to_dict()
        ref.set({"last_login": now}, merge=True)
        role = data.get("role", "user")
        created_at = data.get("created_at", now)
        features = _with_feature_defaults(data.get("features"))
    else:
        role = "admin" if email.lower() in _admin_bootstrap_emails() else "user"
        created_at = now
        features = dict(DEFAULT_FEATURES)
        ref.set({
            "email": email, "role": role, "created_at": created_at,
            "last_login": now, "features": features,
        })
        logger.info("Provisioned new user %s (role=%s)", email, role)

    return {"uid": uid, "email": email, "role": role, "created_at": created_at, "features": features}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    """Returns the caller's own profile. Used by the frontend to decide
    whether to show the Admin page link, without re-provisioning on every
    page load."""
    doc = _profile_ref(user["uid"]).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Not provisioned yet — call POST /api/auth/provision first.")
    data = doc.to_dict()
    data["features"] = _with_feature_defaults(data.get("features"))
    return {"uid": user["uid"], **data}