import os
import sys

# Set up paths to import from backend
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.gmail_spending import _get_gmail_client, _SENDER_HINTS, _SUBJECT_HINTS, _extract_plain_text, _parse_transaction
from app.database import db

def test_gmail():
    # Find a user who has Gmail connected
    users = db.collection("users").stream()
    test_uid = None
    for u in users:
        doc = db.collection("users").document(u.id).collection("integrations").document("gmail").get()
        if doc.exists and doc.to_dict().get("refresh_token"):
            test_uid = u.id
            break
            
    if not test_uid:
        print("No user with connected Gmail found.")
        return

    print(f"Testing for uid: {test_uid}")
    
    service = _get_gmail_client(test_uid)
    if not service:
        print("Failed to get Gmail client.")
        return
        
    sender_query = " OR ".join(f"from:{s}" for s in _SENDER_HINTS)
    subject_query = " OR ".join(f'subject:"{s}"' for s in _SUBJECT_HINTS)
    query = f"({sender_query} OR {subject_query}) newer_than:90d"
    
    print(f"Query: {query}")
    
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=10
    ).execute()
    
    msg_ids = [m["id"] for m in resp.get("messages", [])]
    print(f"Found {len(msg_ids)} messages.")
    
    for mid in msg_ids:
        print(f"\n--- Message {mid} ---")
        full = service.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        received_at = headers.get("Date", "")
        body = _extract_plain_text(full.get("payload", {}))
        
        print(f"Subject: {subject}")
        print(f"Date: {received_at}")
        print(f"Body snippet: {body[:100].strip()}...")
        
        parsed = _parse_transaction(subject, body, received_at)
        if parsed is None:
            print(f"\n--- MISSING Message {mid} ---")
            print(f"Subject: {subject}")
            print(f"Body snippet: {body[:150].strip()}...")

if __name__ == "__main__":
    test_gmail()
