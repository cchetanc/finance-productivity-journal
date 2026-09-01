from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from ..auth import get_current_user_uid
from ..database import (
    append_journal_turn,
    get_journal_ref,
    get_journals_collection,
    list_journals,
    save_journal_summary,
)
from ..agents import journal_reply_async, journal_summary_async

router = APIRouter(prefix="/api/journals", tags=["Journals"])


class JournalEntry(BaseModel):
    title: str
    content: str = ""


class JournalMessage(BaseModel):
    message: str


@router.get("/")
def list_journal_entries(uid: str = Depends(get_current_user_uid)):
    """
    Lists the authenticated user's journals (title, summary, message count),
    newest first. Isolation is enforced entirely by list_journals()/
    get_journals_collection() scoping every query to /users/{uid}/journals.
    """
    return {"journals": list_journals(uid)}


@router.post("/")
def create_journal(entry: JournalEntry, uid: str = Depends(get_current_user_uid)):
    """
    Creates a new journal for the authenticated user. `content`, if given, is
    seeded as the first user turn in the conversation so POST /{id}/chat can
    continue straight from it.
    """
    collection_ref = get_journals_collection(uid)
    doc_data = {"title": entry.title, "messages": []}
    _, doc_ref = collection_ref.add(doc_data)
    if entry.content.strip():
        append_journal_turn(uid, doc_ref.id, "user", entry.content.strip())
    return {"id": doc_ref.id, "message": "Journal entry created successfully"}


@router.get("/{journal_id}")
def get_journal(journal_id: str, uid: str = Depends(get_current_user_uid)):
    """
    Retrieves a specific journal (full message history + latest summary, if
    any) for the authenticated user. get_journal_ref() enforces the
    /users/{uid}/journals/{journalId} path — a journal_id belonging to a
    different uid simply won't be found under this uid's own subcollection.
    """
    doc_ref = get_journal_ref(uid, journal_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return {"id": doc.id, **doc.to_dict()}


async def _summarize_in_background(uid: str, journal_id: str):
    """Runs after the chat response has already been returned to the user —
    the 'background worker' step from docs/architecture_blueprint.md ("a
    background worker requests a concise summary from Gemini and writes the
    data payload directly to the user's isolated Firestore cluster"). Reads
    the journal fresh (rather than closing over the pre-append history) so
    it always summarizes the latest saved state.
    """
    doc = get_journal_ref(uid, journal_id).get()
    if not doc.exists:
        return
    messages = (doc.to_dict() or {}).get("messages", [])
    if not messages:
        return
    summary = await journal_summary_async(messages)
    if summary:
        save_journal_summary(uid, journal_id, summary)


@router.post("/{journal_id}/chat")
async def chat_with_journal(
    journal_id: str,
    body: JournalMessage,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_current_user_uid),
):
    """
    Multi-turn journaling conversation with Gemini. Each call:
      1. Verifies the journal belongs to this uid (get_journal_ref scoping).
      2. Appends the user's new message to the stored conversation.
      3. Replays the FULL conversation history to Gemini (genuine multi-turn —
         see agents.journal_reply_async) and appends the model's reply.
      4. Kicks off summary regeneration as a background task so the user
         isn't stuck waiting on a second Gemini call before getting their
         reply back.
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    doc = get_journal_ref(uid, journal_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    history = append_journal_turn(uid, journal_id, "user", body.message.strip())
    reply_text = await journal_reply_async(history)
    history = append_journal_turn(uid, journal_id, "model", reply_text)

    background_tasks.add_task(_summarize_in_background, uid, journal_id)

    return {"id": journal_id, "reply": reply_text, "message_count": len(history)}


@router.post("/{journal_id}/summarize")
async def summarize_journal(journal_id: str, uid: str = Depends(get_current_user_uid)):
    """
    Manual trigger to (re)generate a journal's summary on demand, in case the
    caller doesn't want to wait for the automatic post-chat background task.
    """
    doc_ref = get_journal_ref(uid, journal_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    messages = (doc.to_dict() or {}).get("messages", [])
    summary = await journal_summary_async(messages)
    if summary:
        save_journal_summary(uid, journal_id, summary)
    return {"id": journal_id, "summary": summary}