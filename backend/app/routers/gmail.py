import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from ..auth import get_current_user_uid
from .. import gmail_spending

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gmail", tags=["Gmail"])

# Where Google redirects back to after the user grants/denies consent — must
# exactly match a redirect URI registered on the OAuth Client ID in GCP.
# Overriding via env var keeps this correct across local/staging/prod without
# a code change.
import os
REDIRECT_URI = os.environ.get("GMAIL_OAUTH_REDIRECT_URI", "http://localhost:8000/api/gmail/oauth-callback")
# Where to send the user's browser back to in the frontend once the callback
# has finished (success or failure) — the Streamlit app's own URL.
FRONTEND_RETURN_URL = os.environ.get("FRONTEND_URL", "http://localhost:8501")


@router.get("/status")
def gmail_status(uid: str = Depends(get_current_user_uid)):
    """Whether this user has already connected Gmail — the frontend uses
    this to decide whether to show 'Connect Gmail' or 'Connected ✓'."""
    return {"connected": gmail_spending.is_gmail_connected(uid)}


@router.get("/auth-url")
def gmail_auth_url(uid: str = Depends(get_current_user_uid)):
    """Returns the Google consent-screen URL for the frontend to open in a
    new tab/window. The user must be logged into this app already (Firebase
    auth) so we know which uid to attach the resulting Gmail grant to."""
    try:
        url = gmail_spending.build_auth_url(uid, REDIRECT_URI)
        return {"auth_url": url}
    except Exception as e:
        log.exception("Failed to build Gmail auth URL")
        raise HTTPException(status_code=500, detail=f"Could not start Gmail connection: {e}")


@router.get("/oauth-callback")
def gmail_oauth_callback(code: str = Query(None), state: str = Query(None), error: str = Query(None)):
    """Google redirects here after the consent screen. `state` is the uid we
    passed into build_auth_url. This endpoint is NOT behind Firebase auth —
    it can't be, since it's Google calling us, not the logged-in browser
    session — so the uid comes only from `state`, never a client-suppliable
    body field, and the whole point of `state` is that it was generated
    server-side for a specific already-authenticated user in /auth-url."""
    if error:
        return RedirectResponse(f"{FRONTEND_RETURN_URL}?gmail_connect=error&reason={error}")
    if not code or not state:
        return RedirectResponse(f"{FRONTEND_RETURN_URL}?gmail_connect=error&reason=missing_code")
    try:
        gmail_spending.handle_oauth_callback(code, uid=state, redirect_uri=REDIRECT_URI)
    except Exception as e:
        log.exception("Gmail OAuth callback failed")
        return RedirectResponse(f"{FRONTEND_RETURN_URL}?gmail_connect=error&reason={type(e).__name__}")
    return RedirectResponse(f"{FRONTEND_RETURN_URL}?gmail_connect=success")


@router.post("/disconnect")
def gmail_disconnect(uid: str = Depends(get_current_user_uid)):
    gmail_spending.disconnect_gmail(uid)
    return {"disconnected": True}


@router.post("/sync")
def gmail_sync(
    days_back: int = 90,
    force_reparse: bool = False,
    uid: str = Depends(get_current_user_uid),
):
    """Triggers a scan of recent Gmail for UPI/bank debit alerts and stores
    parsed transactions. Call this on-demand (e.g. a 'Refresh spending data'
    button) rather than on every chat turn — a full scan is too slow to run
    inline while the user is waiting on a chat reply.

    Pass force_reparse=true to re-parse messages that were already synced,
    overwriting their stored amount/merchant with the current parsing logic
    instead of only picking up new mail. Use this once to correct data
    synced before a parsing-logic fix; leave it false for routine refreshes
    so they stay fast and incremental."""
    if not gmail_spending.is_gmail_connected(uid):
        raise HTTPException(status_code=400, detail="Gmail isn't connected for this account yet.")
    try:
        stored = gmail_spending.fetch_and_store_upi_transactions(
            uid, days_back=days_back, force_reparse=force_reparse
        )
        return {"new_transactions_stored": stored}
    except Exception as e:
        log.exception("Gmail sync failed")
        raise HTTPException(status_code=500, detail=f"Gmail sync failed: {e}")


@router.get("/spending-summary")
def gmail_spending_summary(month: str = None, uid: str = Depends(get_current_user_uid)):
    """Returns already-synced monthly totals (call /sync first / periodically
    to keep this fresh — this endpoint only reads what's already stored)."""
    if not gmail_spending.is_gmail_connected(uid):
        raise HTTPException(status_code=400, detail="Gmail isn't connected for this account yet.")
    return gmail_spending.get_monthly_spending_summary(uid, month=month)