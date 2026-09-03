"""
Gmail UPI-spending integration.

Lets a user connect their Gmail account (read-only) so the assistant can
answer "how did my spending look this month" style questions by scanning
bank/UPI-app debit-notification emails and aggregating the amounts.

THIS IS A SEPARATE GRANT FROM FIREBASE LOGIN. Firebase auth (see auth.py)
only proves who the user is to this app's own API — it does NOT hand this
backend any access to the user's Gmail. Reading Gmail requires its own
Google OAuth 2.0 "installed/web app" consent screen with the
`gmail.readonly` scope, which the user must explicitly grant once via the
`/api/gmail/auth-url` -> Google consent -> `/api/gmail/oauth-callback` flow
implemented below. Nothing here reads Gmail without that explicit grant.

Setup required before this works (not code changes — Google Cloud config):
  1. In the same GCP project, create an OAuth 2.0 Client ID (type: Web
     application) in "APIs & Services > Credentials", with the deployed
     backend's callback URL (e.g. https://<backend>/api/gmail/oauth-callback)
     added as an authorized redirect URI.
  2. Enable the "Gmail API" for the project.
  3. Store the client id/secret as Secret Manager secrets
     GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET (same pattern as
     every other secret in secrets.py) rather than env vars, so the client
     secret is never committed or logged.
  4. `pip install google-auth google-auth-oauthlib google-api-python-client`
     (add to backend/requirements.txt).

Data isolation: the refresh token and every parsed transaction are stored
under /users/{uid}/integrations/gmail and /users/{uid}/upi_transactions,
following the same per-tenant Firestore path convention as journals.py /
database.py — never a shared/global collection.
"""
import base64
import datetime
import logging
import re
import email.utils
from collections import defaultdict

from .database import db
from .secrets import access_secret_version

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Only UPI/bank debit-alert style mail is in scope — deliberately narrow so
# this never becomes a general "read all my email" feature. Extend this list
# rather than widening the Gmail search query below if a bank/app isn't
# being picked up.
_SENDER_HINTS = (
    "alerts@hdfcbank.net", "alerts@icicibank.com", "unify.axisbank.com", "axis.bank.in",
    "sbi.co.in", "kotak.com", "ybl.axisbank.com", "paytm.com",
    "phonepe.com", "okaxis", "@upi.npci.org.in", "googlepay.com",
)
_SUBJECT_HINTS = ("UPI", "debited", "payment successful", "transaction alert", "amount debited")

# Matches "Rs.1,234.50", "INR 500", "Rs 99" etc. with an optional decimal part.
_AMOUNT_RE = re.compile(r"(?:Rs\.?|INR|₹)\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
# A handful of common phrasings banks/UPI apps actually use for a DEBIT
# (money leaving the account) — kept separate from credit-alert phrasing so
# incoming money isn't double-counted as spend.
_DEBIT_KEYWORDS = re.compile(
    r"\b(debited|paid|payment of|spent|sent to|withdrawn)\b", re.IGNORECASE
)
_CREDIT_KEYWORDS = re.compile(r"\b(credited|received from|refund of)\b", re.IGNORECASE)

# --- Merchant/payee extraction -------------------------------------------
#
# Earlier version of this file ran a single generic "to <name>" / "at <name>"
# regex over the ENTIRE subject+body and took the first hit. That's wrong in
# two ways that showed up in production:
#   1. Real bank/UPI narrations often DON'T use "to"/"at" at all (e.g. HDFC's
#      "Info: UPI-SWIGGY-swiggy@ybl-...", or "VPA merchant@icici"), so the
#      generic pattern skipped right past the real payee.
#   2. Every alert email also contains boilerplate elsewhere in the body —
#      "this is to inform you that...", "we're here to help you", "SMS BLOCK
#      to 7308080808" — which DOES match "to <words>"/"at <words>" and, being
#      searched over the whole email, was very often what actually got
#      captured instead of the merchant. That's why summaries showed
#      merchant buckets like "help you" or "inform you that INR 4579"
#      (the amount digits themselves got swept into the capture group) and a
#      support/helpline number masquerading as a top merchant.
#
# Fix: (a) only look for a merchant inside a narrow window around the amount
# — real narrations always sit right next to the amount, disclaimers/footers
# are a separate sentence/paragraph away — and (b) try bank-specific
# patterns (VPA, hyphen-delimited UPI narration, explicit "trf/paid to")
# before falling back to the old generic pattern, validating every candidate
# against a blocklist of boilerplate phrases before accepting it.
_MERCHANT_WINDOW_BEFORE = 120
_MERCHANT_WINDOW_AFTER = 160

# Trailing words that mean "the payee name has ended here" — shared across
# patterns so a merchant capture doesn't run on into the next clause (e.g.
# "...to Reliance Digital using UPI" should stop before "using").
_MERCHANT_STOP_WORDS = r"(?:ref\.?\s*no\.?|refno|rrn|upi\s*ref\w*|on|via|using|thru|through|thank\w*)"

_MERCHANT_PATTERNS = (
    # "VPA merchantname@icici (PAYEE NAME)" / "UPI ID x@ybl (Payee Name)" —
    # tried FIRST and ahead of the bare-VPA-handle pattern below, because
    # when a bank narration includes the human payee name in parentheses
    # right after the VPA/handle (very common — HDFC, ICICI, Axis all do
    # this: "...towards VPA paytm.s2flo51@pty (PULASI KURUMAIAH) on..."),
    # that name is far more useful in a spending summary than the raw UPI
    # handle prefix ("paytm.s2flo51") the next pattern down would otherwise
    # grab. Requires at least one space in the capture (a real name has
    # one) so a stray single all-caps acronym in parens elsewhere isn't
    # mistaken for a payee.
    re.compile(r"@[A-Za-z]{2,15}\s*\(([A-Za-z][A-Za-z .'-]*\s[A-Za-z][A-Za-z .'-]*)\)"),
    # "VPA merchantname@icici" / "UPI ID merchant.name@ybl"
    re.compile(r"(?:VPA|UPI\s*ID)[:\s]+([A-Za-z0-9.\-_]{2,40})@[A-Za-z]{2,15}", re.IGNORECASE),
    # Axis/SBI-style "Transaction Info: UPI/P2M/<12-digit ref>/<PAYEE NAME>"
    # — the payee is the LAST slash-delimited segment, after a transaction-
    # type code (P2M/P2A/etc.) and a numeric reference. Tried BEFORE the
    # shorter "UPI/<name>-..." pattern below, which would otherwise match
    # first and wrongly capture the transaction-type code ("P2M") itself as
    # the merchant — that's exactly the bug that showed "P2M" as a top
    # merchant in production instead of "MOHD AKBAR" / "N CAFE".
    re.compile(
        rf"\bUPI/[A-Za-z0-9]+/\d{{4,}}/([A-Za-z][\w .&'-]{{1,40}}?)(?:\s+{_MERCHANT_STOP_WORDS}\b|[.,\n]|$)",
        re.IGNORECASE,
    ),
    # HDFC/Kotak-style hyphen or slash delimited narration: "UPI-SWIGGY-..."
    # — tried before the bare-handle pattern below so a hyphenated narration
    # yields the clean "SWIGGY" segment rather than getting picked up whole.
    re.compile(r"\bUPI[/-]([A-Za-z][A-Za-z0-9 &.'-]{1,30}?)(?=[/-])", re.IGNORECASE),
    # Any bare "handle@bank" token near the amount, even without a literal
    # "VPA"/"UPI ID" label in front of it (e.g. "...credited to
    # merchantname@icici (UPI Ref no...)") — '@' can't appear in the
    # word-character classes the other patterns use, so this needs its own
    # pattern. No hyphens in the class (unlike above) so this can't swallow
    # a whole "UPI-SWIGGY-swiggy@ybl" style narration in one bite — it
    # naturally lands on just the "swiggy" segment right before '@'.
    re.compile(r"\b([A-Za-z][A-Za-z0-9._]{1,30})@[a-zA-Z]{2,15}\b"),
    # Explicit "trf to X" / "transfer to X" / "paid to X" / "payment to X",
    # optionally with the amount sitting between the verb and "to" (e.g.
    # "paid Rs.500 to Merchant").
    re.compile(
        r"\b(?:trf|transfer|paid|payment(?:\s+of)?|sent)\s+"
        r"(?:(?:Rs\.?|INR|₹)\s?[\d,]+(?:\.\d{1,2})?\s+)?(?:to|at)\s+"
        rf"([A-Za-z][\w .&'-]{{1,40}}?)(?:\s+{_MERCHANT_STOP_WORDS}\b|[.,\n]|$)",
        re.IGNORECASE,
    ),
    # Generic fallback — same shape as the original regex, but only ever
    # run inside the narrow amount-window, never the whole email.
    re.compile(
        rf"\b(?:to|at)\s+([A-Za-z0-9][\w .&'-]{{1,40}}?)(?:\s+{_MERCHANT_STOP_WORDS}\b|[.,\n]|$)",
        re.IGNORECASE,
    ),
)
# Whether an all-digit capture is acceptable for each pattern above, in the
# same order — a phone-number-style UPI ID is unambiguous right after
# "VPA"/"trf to", but not from the looser generic fallback pattern. The
# new leading parenthetical-name pattern can never match a pure-digit
# candidate in the first place (its class requires letters + a space), so
# its flag value doesn't matter, but it's listed for positional clarity.
_MERCHANT_PATTERN_ALLOW_NUMERIC = (False, True, False, False, True, True, False)

# Phrases that are almost never a real merchant name — customer-care /
# disclaimer boilerplate that the generic "to/at" pattern is prone to
# grabbing. Checked as a substring against the lowercased candidate.
_MERCHANT_BLOCKLIST = (
    "help you", "inform you", "know more", "contact us", "customer care",
    "customer service", "report this", "protect yourself", "avoid fraud",
    "raise a dispute", "block your card", "block upi", "verify your",
    "unsubscribe", "download the app", "visit our", "click here",
    "the best of", "your account", "www.", "http", "grievance",
    "not done by you", "not you", "sms block", "call us", "toll free",
    "register your complaint", "report fraud", "immediately", "dial ",
)
# Trailing reference-number cruft that sometimes rides along with an
# otherwise-good match, e.g. "MERCHANT NAME Refno 123456789012".
_MERCHANT_TRAILING_REF_RE = re.compile(
    r"\b(?:ref\.?\s*no\.?|refno|rrn|upi\s*ref(?:erence)?(?:\s*no)?)\b.*$", re.IGNORECASE
)


def _clean_merchant_candidate(raw: str, allow_numeric: bool = False) -> str | None:
    """Validates and normalizes a regex-captured merchant candidate.
    Returns None if it looks like boilerplate/noise rather than a payee.

    allow_numeric: a candidate with no letters at all (e.g. "9876543210")
    is only accepted for patterns where an all-digit match unambiguously
    means a phone-number-style UPI ID (VPA / explicit "trf to" fields) —
    for the looser generic fallback pattern it's far more likely to be a
    stray reference/helpline number, so it's rejected there."""
    candidate = _MERCHANT_TRAILING_REF_RE.sub("", raw).strip(" .-_")
    if not candidate:
        return None
    lowered = candidate.lower()
    if any(phrase in lowered for phrase in _MERCHANT_BLOCKLIST):
        return None
    # Guards against the amount itself (or currency words) leaking into the
    # capture group, e.g. "inform you that INR 4579" -> candidate containing
    # "inr"/"rs"/"₹" immediately followed by digits.
    if re.search(r"\b(?:inr|rs|₹|rupees)\b", lowered):
        return None
    if not re.search(r"[A-Za-z]", candidate) and not allow_numeric:
        # Purely numeric (phone number / ref code) with no letters at all —
        # too ambiguous to label as a merchant for this pattern.
        return None
    # Collapse internal whitespace, keep original casing (bank narrations
    # are often already in a sensible case; we don't force title-case since
    # that mangles things like "PVR" or "IRCTC").
    return re.sub(r"\s+", " ", candidate).strip()


def _get_oauth_client_config(redirect_uri: str) -> dict:
    client_id = access_secret_version("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = access_secret_version("GOOGLE_OAUTH_CLIENT_SECRET")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def build_auth_url(uid: str, redirect_uri: str) -> str:
    """Returns the Google consent-screen URL to send the user's browser to.
    `state` carries the uid so the callback knows whose account to attach
    the resulting tokens to."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _get_oauth_client_config(redirect_uri), scopes=GMAIL_SCOPES, redirect_uri=redirect_uri
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",       # request a refresh_token, not just a short-lived access token
        include_granted_scopes="true",
        prompt="consent",            # force a fresh refresh_token even on a repeat connect
        state=uid,
    )
    return auth_url


def handle_oauth_callback(code: str, uid: str, redirect_uri: str) -> None:
    """Exchanges the consent-flow's one-time code for tokens and stores the
    refresh token (the only piece needed long-term) under this user's own
    Firestore doc. The refresh token itself is sensitive — anyone holding it
    can pull this user's Gmail — so nothing here logs it, and Firestore's own
    IAM/security rules (see config/firestore.rules) are what actually gate
    read access to this document, same as every other per-user doc in the
    app."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _get_oauth_client_config(redirect_uri), scopes=GMAIL_SCOPES, redirect_uri=redirect_uri
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    db.collection("users").document(uid).collection("integrations").document("gmail").set({
        "refresh_token": creds.refresh_token,
        "connected_at": datetime.datetime.utcnow().isoformat(),
        "scopes": GMAIL_SCOPES,
    }, merge=True)


def is_gmail_connected(uid: str) -> bool:
    doc = db.collection("users").document(uid).collection("integrations").document("gmail").get()
    return doc.exists and bool((doc.to_dict() or {}).get("refresh_token"))


def disconnect_gmail(uid: str) -> None:
    db.collection("users").document(uid).collection("integrations").document("gmail").delete()


def _get_gmail_client(uid: str):
    """Builds an authorized Gmail API client from the user's stored refresh
    token, refreshing the access token as needed. Returns None if the user
    hasn't connected Gmail."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    doc = db.collection("users").document(uid).collection("integrations").document("gmail").get()
    if not doc.exists:
        return None
    refresh_token = (doc.to_dict() or {}).get("refresh_token")
    if not refresh_token:
        return None

    client_id = access_secret_version("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = access_secret_version("GOOGLE_OAUTH_CLIENT_SECRET")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_plain_text(payload: dict) -> str:
    """Gmail messages can be multipart; walk the parts for a text/plain (or
    fall back to text/html stripped of tags) body, base64url-decoded."""
    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "ignore")
        except Exception:
            return ""

    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])

    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text

    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = _decode(payload["body"]["data"])
        return re.sub(r"<[^>]+>", " ", html)

    return ""


def _parse_transaction(subject: str, body: str, received_at: str) -> dict | None:
    """Best-effort extraction of (amount, merchant, is_debit) from a single
    email's subject+body. Returns None if this doesn't look like a genuine
    debit notification (e.g. it's a credit, or no amount could be found) —
    callers should skip rather than guess."""
    text = f"{subject}\n{body}"
    if _CREDIT_KEYWORDS.search(text) and not _DEBIT_KEYWORDS.search(text):
        return None
    if not _DEBIT_KEYWORDS.search(text):
        return None

    amount_match = _AMOUNT_RE.search(text)
    if not amount_match:
        return None
    try:
        amount = float(amount_match.group(1).replace(",", ""))
    except ValueError:
        return None

    # Only look for the merchant in a narrow window around the amount — the
    # real payee narration always sits right next to it; boilerplate like
    # helpline numbers or "to inform you" disclaimers live elsewhere in the
    # email and are excluded by not being in this window at all.
    window_start = max(0, amount_match.start() - _MERCHANT_WINDOW_BEFORE)
    window_end = min(len(text), amount_match.end() + _MERCHANT_WINDOW_AFTER)
    window = text[window_start:window_end]

    merchant = "Unknown"
    for pattern, allow_numeric in zip(_MERCHANT_PATTERNS, _MERCHANT_PATTERN_ALLOW_NUMERIC):
        # Try every match of this pattern in the window, not just the
        # first — a boilerplate phrase earlier in the window (e.g. "...is
        # to inform you...") shouldn't shadow a real payee mentioned later
        # in the same window.
        for m in pattern.finditer(window):
            cleaned = _clean_merchant_candidate(m.group(1), allow_numeric=allow_numeric)
            if cleaned:
                merchant = cleaned
                break
        if merchant != "Unknown":
            break

    return {"amount": amount, "merchant": merchant, "received_at": received_at, "subject": subject}


def fetch_and_store_upi_transactions(
    uid: str, days_back: int = 90, max_messages: int = 300, force_reparse: bool = False
) -> int:
    """Pulls recent UPI/bank debit-alert emails, parses them, and upserts
    them into /users/{uid}/upi_transactions (keyed by Gmail message id so a
    re-scan doesn't duplicate rows). Returns how many transactions were
    (re-)stored. Call this from a background task or before answering a
    spending-summary question, rather than on every chat turn — a full
    Gmail scan is too slow to run inline per message.

    force_reparse=True re-fetches and re-parses messages that were already
    stored, overwriting their saved fields with the current parsing logic.
    Only stored fields (subject/body) come from Gmail — nothing here can fix
    old rows without a network call, since we deliberately don't persist
    raw email bodies. Use this once after a parsing-logic change (e.g. the
    merchant-extraction fix) to correct previously-mis-parsed merchants
    without waiting for `days_back` worth of new mail to arrive; normal
    scheduled/inline syncs should keep force_reparse=False so they stay
    cheap and incremental."""
    service = _get_gmail_client(uid)
    if service is None:
        raise RuntimeError("Gmail not connected for this user.")

    sender_query = " OR ".join(f"from:{s}" for s in _SENDER_HINTS)
    subject_query = " OR ".join(f'subject:"{s}"' for s in _SUBJECT_HINTS)
    # A bare quoted term (no "subject:" prefix) searches the WHOLE message,
    # not just the subject line — plenty of real debit alerts only mention
    # "UPI" in the body (e.g. subject is just "Debit Alert" / "Transaction
    # Alert"), so restricting to subject_query alone was missing those.
    # Kept as an OR alongside sender/subject rather than replacing them, so
    # it only ever widens the match set.
    query = f'({sender_query} OR {subject_query} OR "UPI") newer_than:{days_back}d'

    stored = 0
    coll = db.collection("users").document(uid).collection("upi_transactions")
    page_token = None
    fetched = 0
    while fetched < max_messages:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=min(100, max_messages - fetched)
        ).execute()
        msg_ids = [m["id"] for m in resp.get("messages", [])]
        fetched += len(msg_ids)

        for mid in msg_ids:
            doc_ref = coll.document(mid)
            if doc_ref.get().exists and not force_reparse:
                continue  # already parsed on a previous scan
            full = service.users().messages().get(userId="me", id=mid, format="full").execute()
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "")
            received_at = headers.get("Date", "")
            body = _extract_plain_text(full.get("payload", {}))
            parsed = _parse_transaction(subject, body, received_at)
            if parsed:
                doc_ref.set({**parsed, "message_id": mid})
                stored += 1
            elif force_reparse and doc_ref.get().exists:
                # No longer looks like a genuine debit under the current
                # logic (e.g. it was mis-classified before) — drop it
                # rather than leave stale/incorrect data behind.
                doc_ref.delete()

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return stored


def get_monthly_spending_summary(uid: str, month: str | None = None) -> dict:
    """Reads already-parsed transactions from Firestore (call
    fetch_and_store_upi_transactions first / periodically to keep it fresh)
    and aggregates them by calendar month. `month` is "YYYY-MM"; if omitted,
    returns every month found, most recent first."""
    txns = [d.to_dict() for d in db.collection("users").document(uid).collection("upi_transactions").stream()]

    # merchants dict is keyed by a normalized (lowercased/whitespace-
    # collapsed) form so casing variants of the same payee ("Swiggy" /
    # "SWIGGY" / "swiggy ") group together instead of fragmenting the
    # top-merchants list; we keep one display label (the first-seen
    # casing) per normalized key.
    by_month = defaultdict(lambda: {
        "total": 0.0, "count": 0,
        "merchants": defaultdict(float), "merchant_labels": {},
    })
    for t in txns:
        received = t.get("received_at") or ""
        try:
            dt = email.utils.parsedate_to_datetime(received)
        except Exception:
            continue
        key = dt.strftime("%Y-%m")
        if month and key != month:
            continue
        amount = t.get("amount", 0.0)
        raw_merchant = t.get("merchant") or "Unknown"
        norm_key = re.sub(r"\s+", " ", raw_merchant).strip().lower()
        by_month[key]["total"] += amount
        by_month[key]["count"] += 1
        by_month[key]["merchants"][norm_key] += amount
        by_month[key]["merchant_labels"].setdefault(norm_key, raw_merchant)

    result = []
    for key in sorted(by_month.keys(), reverse=True):
        m = by_month[key]
        top_merchants = sorted(m["merchants"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        result.append({
            "month": key,
            "total_spent": round(m["total"], 2),
            "transaction_count": m["count"],
            "top_merchants": [
                {"merchant": m["merchant_labels"][mk], "amount": round(mv, 2)}
                for mk, mv in top_merchants
            ],
        })
    return {"months": result}