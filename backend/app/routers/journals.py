from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from ..auth import get_current_user_uid
from ..database import get_journals_collection, get_journal_ref

router = APIRouter(prefix="/api/journals", tags=["Journals"])

class JournalEntry(BaseModel):
    title: str
    content: str

@router.post("/")
def create_journal(entry: JournalEntry, uid: str = Depends(get_current_user_uid)):
    """
    Creates a new journal entry for the authenticated user.
    """
    collection_ref = get_journals_collection(uid)
    _, doc_ref = collection_ref.add(entry.dict())
    return {"id": doc_ref.id, "message": "Journal entry created successfully"}

@router.get("/{journal_id}")
def get_journal(journal_id: str, uid: str = Depends(get_current_user_uid)):
    """
    Retrieves a specific journal entry for the authenticated user.
    """
    doc_ref = get_journal_ref(uid, journal_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return {"id": doc.id, **doc.to_dict()}
