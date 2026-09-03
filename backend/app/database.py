from google.cloud import firestore
import json
from enum import Enum

db = firestore.Client()

class BrokerType(str, Enum):
    ANGEL_ONE = "ANGEL_ONE"
    HDFC_SEC = "HDFC_SEC"
    PAPER_TRADING = "PAPER_TRADING"

def get_journal_ref(uid: str, journal_id: str):
    """
    Returns a document reference for a specific journal.
    Enforces tenant isolation by restricting path to /users/{uid}/journals/{journalId}
    """
    return db.collection("users").document(uid).collection("journals").document(journal_id)

def get_journals_collection(uid: str):
    """
    Returns the journals collection reference for a user.
    """
    return db.collection("users").document(uid).collection("journals")


def append_journal_turn(uid: str, journal_id: str, role: str, text: str) -> list:
    """
    Appends one {"role", "text"} turn to a journal's stored conversation and
    returns the updated, full message history. Path stays isolated under
    /users/{uid}/journals/{journalId} per the Data Isolation Matrix in
    docs/architecture_blueprint.md.
    """
    import datetime
    doc_ref = get_journal_ref(uid, journal_id)
    doc = doc_ref.get()
    existing = doc.to_dict() if doc.exists else {}
    messages = existing.get("messages", [])
    messages.append({
        "role": role,
        "text": text,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    })
    doc_ref.set({**existing, "messages": messages}, merge=True)
    return messages


def list_journals(uid: str, limit: int = 50) -> list:
    """
    Returns the user's journals, newest first, with a lightweight preview
    (title, summary, message count) rather than the full message history —
    keeps the list view cheap.
    """
    query = (
        get_journals_collection(uid)
        .order_by("summary_updated_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    try:
        docs = list(query.stream())
    except Exception:
        # summary_updated_at may not exist yet on older docs / an unindexed
        # field — fall back to an unordered read rather than erroring out.
        docs = list(get_journals_collection(uid).limit(limit).stream())
    out = []
    for doc in docs:
        data = doc.to_dict() or {}
        out.append({
            "id": doc.id,
            "title": data.get("title"),
            "summary": data.get("summary"),
            "message_count": len(data.get("messages", [])),
        })
    return out


def save_journal_summary(uid: str, journal_id: str, summary: str):
    """
    Writes the background-worker-generated summary onto the journal doc, per
    the 'Summarization Process' step in docs/architecture_blueprint.md.
    """
    import datetime
    get_journal_ref(uid, journal_id).set(
        {"summary": summary, "summary_updated_at": datetime.datetime.utcnow().isoformat()},
        merge=True,
    )

def get_daily_chat_ref(uid: str, date_str: str):
    """
    Returns a document reference for one calendar day's Daily Productivity
    Assistant chat. Path: /users/{uid}/daily_chat/{YYYY-MM-DD} (IST date).
    Crossing midnight IST means the next message lands in a brand-new doc —
    that's what makes "today starts fresh, yesterday stays put" work without
    any explicit archiving/cleanup step.
    """
    return db.collection("users").document(uid).collection("daily_chat").document(date_str)


def get_daily_chat(uid: str, date_str: str) -> dict:
    """
    Returns {"messages": [...], "phases_sent": [...]} for the given day,
    or empty defaults if nothing has been stored yet for that day.
    """
    doc = get_daily_chat_ref(uid, date_str).get()
    if not doc.exists:
        return {"messages": [], "phases_sent": []}
    data = doc.to_dict() or {}
    return {
        "messages": data.get("messages", []),
        "phases_sent": data.get("phases_sent", []),
    }


def append_daily_chat_message(uid: str, date_str: str, role: str, text: str, route: dict | None = None) -> list:
    """
    Appends one {"role", "text", "timestamp"} turn to today's stored chat
    and returns the updated, full message list for that day. `role` is
    "user" or "cfa" (matches the frontend's voice_history shape so it can
    be loaded back in directly without any translation).
    """
    import datetime
    doc_ref = get_daily_chat_ref(uid, date_str)
    doc = doc_ref.get()
    existing = doc.to_dict() if doc.exists else {}
    messages = existing.get("messages", [])
    entry = {
        "role": role,
        "text": text,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
    if route:
        entry["route"] = route
    messages.append(entry)
    doc_ref.set({**existing, "messages": messages}, merge=True)
    return messages


def mark_daily_chat_phase_sent(uid: str, date_str: str, phase: str):
    """
    Records that the proactive greeting for a given market 'phase'
    (PRE_MARKET / MARKET_HOURS / POST_MARKET / WEEKEND) has already been
    sent today, so the panel doesn't push the same kind of proactive
    message again on every reconnect within the same phase.
    """
    doc_ref = get_daily_chat_ref(uid, date_str)
    doc = doc_ref.get()
    existing = doc.to_dict() if doc.exists else {}
    phases_sent = existing.get("phases_sent", [])
    if phase not in phases_sent:
        phases_sent.append(phase)
    doc_ref.set({**existing, "phases_sent": phases_sent}, merge=True)


def get_trade_ref(uid: str, trade_id: str):
    """
    Returns a document reference for a specific trade.
    Enforces tenant isolation by restricting path to /users/{uid}/trades/{tradeId}
    """
    return db.collection("users").document(uid).collection("trades").document(trade_id)

def get_trades_collection(uid: str):
    """
    Returns the trades collection reference for a user.
    """
    return db.collection("users").document(uid).collection("trades")

def get_broker_config(uid: str):
    """
    Retrieves the broker execution configuration for a user.
    Handles the UI selector mapping.
    """
    doc_ref = db.collection("users").document(uid).collection("config").document("broker")
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return {"active_broker": BrokerType.PAPER_TRADING.value}

def save_market_signals(uid: str, signals_payload: list):
    """
    Takes the parsed JSON payload returned by Gemini and saves it directly into Cloud Firestore.
    Adheres strictly to the data isolation rules by using the authenticated uid.
    Path: /users/{uid}/market_signals/{signalId}
    """
    signals_collection = db.collection("users").document(uid).collection("market_signals")
    batch = db.batch()
    
    saved_ids = []
    for signal in signals_payload:
        doc_ref = signals_collection.document()
        batch.set(doc_ref, signal)
        saved_ids.append(doc_ref.id)
        
    batch.commit()
    return saved_ids

def get_algo_executions_collection(uid: str):
    """
    Returns the algo executions collection reference for a user.
    Path: /users/{uid}/algo_executions/{executionId}
    """
    return db.collection("users").document(uid).collection("algo_executions")


def list_algo_executions(uid: str, limit: int = 50):
    """
    Returns the user's most recent algo executions, newest first.
    """
    query = (
        get_algo_executions_collection(uid)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


def list_trades(uid: str, limit: int = 100):
    """
    Returns the user's most recent manually-placed order receipts (i.e. from
    POST /api/trading/orders — not algo executions), newest first.
    """
    query = (
        get_trades_collection(uid)
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


def save_trade_execution(uid: str, execution_data: dict):
    """
    Saves a completed trade execution receipt to Firestore.
    Path: /users/{uid}/trades/{tradeId}
    """
    trades_collection = get_trades_collection(uid)
    doc_ref = trades_collection.document()
    
    # Add timestamp
    import datetime
    execution_data["timestamp"] = datetime.datetime.utcnow().isoformat()
    
    doc_ref.set(execution_data)
    return doc_ref.id