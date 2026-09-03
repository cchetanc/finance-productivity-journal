"""
Role-based admin endpoints. Currently: list every provisioned user, for the
"which new sign-ups do we have" dashboard.

Security model:
  - config/firestore.rules restricts ANY direct client Firestore access to
    a user's own /users/{uid} subtree — that boundary is untouched by this
    file and stays true for every other collection in the app.
  - This router only works at all because the backend connects to Firestore
    with its own service-account credentials (see database.py's
    firestore.Client()), which security rules simply don't apply to — the
    Admin SDK/server client is inherently privileged.
  - Because of that, EVERY route in this router re-checks the caller's role
    itself (require_admin, below) by reading their own profile doc fresh
    from Firestore on every call. It never trusts a role claimed by the
    client (e.g. a value cached in the frontend's session_state) — that
    value is just a UI convenience, not a security boundary.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..database import db
from .auth import DEFAULT_FEATURES, _with_feature_defaults

logger = logging.getLogger("admin.router")
router = APIRouter(prefix="/api/admin", tags=["Admin"])


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    doc = db.collection("users").document(user["uid"]).collection("profile").document("info").get()
    if not doc.exists or doc.to_dict().get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


@router.get("/users")
def list_users(limit: int = 200, _admin: dict = Depends(require_admin)):
    """
    Lists every provisioned user's PROFILE ONLY (email, role, created_at,
    last_login, home-page feature flags) — never journals, trades, chat
    history, or broker config, all of which stay in that user's own
    isolated subtree and are simply never queried here. Uses a Firestore
    collection-group query across every /users/*/profile/info doc.
    """
    users = []
    for doc in db.collection_group("profile").limit(limit).stream():
        data = doc.to_dict()
        # doc.reference is .../users/{uid}/profile/info — parent.parent is
        # the /users/{uid} document, whose id is the uid.
        uid = doc.reference.parent.parent.id
        users.append({
            "uid": uid,
            "email": data.get("email"),
            "role": data.get("role", "user"),
            "created_at": data.get("created_at"),
            "last_login": data.get("last_login"),
            "features": _with_feature_defaults(data.get("features")),
        })
    users.sort(key=lambda u: u.get("created_at") or "", reverse=True)
    return {"users": users, "total": len(users)}


class FeaturesUpdate(BaseModel):
    daily_productivity: bool = True
    news: bool = False
    entertainment: bool = False
    equity_news: bool = False
    smart_investor: bool = False


@router.post("/users/{uid}/features")
def set_user_features(uid: str, body: FeaturesUpdate, _admin: dict = Depends(require_admin)):
    """
    Admin-only: sets exactly which home-page sections a given user can see
    (see routers/auth.py's DEFAULT_FEATURES for what each flag controls).
    Only touches the `features` map on that user's own profile doc — role,
    email, and login timestamps are untouched.
    """
    ref = db.collection("users").document(uid).collection("profile").document("info")
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="No such user.")
    features = body.dict()
    ref.set({"features": features}, merge=True)
    return {"uid": uid, "features": features}