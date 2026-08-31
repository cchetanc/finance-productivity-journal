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