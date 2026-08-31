from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List
import base64
from ..gemini import analyze_indian_market_news, SmartOrderRouter, batch_analyze_headlines, CFAMultiAgentBot
from ..sentiment import batch_classify_headlines
from ..auth import get_current_user_uid
from ..database import get_broker_config, save_trade_execution
from ..market_data import get_live_indices, compute_market_mood, get_live_news, get_categorized_news, get_entertainment_releases, get_precious_metal_rates, INDEX_GROUPS

router = APIRouter(prefix="/api/market", tags=["Market"])


class NewsPayload(BaseModel):
    text: str

class HistoryTurn(BaseModel):
    role: str   # "user" or "assistant"
    text: str

class VoicePayload(BaseModel):
    prompt: str = ""
    audio_in_base64: Optional[str] = None
    persona: str = "Aoede"
    session_id: str = "default"
    mode: str = "TEXT" # TEXT or VOICE — whether the reply should include synthesized audio
    location: Optional[str] = None  # e.g. "17.4239,78.4738" or a reverse-geocoded area name
    # Last few turns of the conversation, oldest first. Without this, every
    # message is routed in total isolation — a reply like "Kondapur in
    # Hyderabad" to "which city are you in?" has no way to be recognized as
    # a leisure follow-up and gets routed as a bare, context-free query
    # (which reads to the router like a real-estate location lookup).
    history: Optional[List[HistoryTurn]] = None


class TradePayload(BaseModel):
    ticker: str
    quantity: int
    price: float
    current_market_price: float


@router.get("/mood")
def get_market_mood():
    """
    Returns the live systemic market mood score derived from real index data.
    Also returns INDEX_GROUPS metadata for grouped frontend display.
    """
    indices = get_live_indices()
    mood    = compute_market_mood(indices)
    return {
        "systemic_score":    mood["systemic_score"],
        "bias":              mood["bias"],
        "macro_risk_flags":  mood["macro_risk_flags"],
        "indices":           indices,
        "index_groups":      INDEX_GROUPS,
    }


@router.get("/news")
def get_market_news():
    """
    Fetches live raw RSS headlines quickly (no AI blocking).
    """
    headlines = get_live_news(limit=12)
    return {"headlines": headlines}


@router.get("/news/categorized")
def get_categorized_market_news(city: str = None):
    """
    Global / India / Local general-news headlines for the dashboard's news
    panel (distinct from the finance-only /news above). 'city' is the
    browser's reverse-geocoded location string, if the user granted it —
    the 'local' tier comes back empty without one.
    """
    return get_categorized_news(city=city, limit_each=4)


@router.get("/entertainment")
def get_entertainment_market():
    """
    Movie & series release headlines for the dashboard's 'New Releases'
    tile — theatres (box office) plus OTT platforms (Netflix, Prime Video,
    JioHotstar/Disney+Hotstar, Zee5).
    """
    return get_entertainment_releases(limit_each=4)


@router.get("/precious-metals")
def precious_metals():
    """
    Gold/silver rates for the header pills — see get_precious_metal_rates()
    docstring for the international-spot-proxy caveat.
    """
    return get_precious_metal_rates()


@router.get("/news/sentiment")
def get_news_sentiment(titles: str):
    """
    Classifies news headlines using ProsusAI/FinBERT — purpose-trained on financial text.
    Returns BULLISH / BEARISH / NEUTRAL for each headline.
    """
    title_list = [t.strip() for t in titles.split("|||") if t.strip()]
    if not title_list:
        return {"sentiments": {}}
    # Use FinBERT for accurate financial sentiment
    sentiment_map = batch_classify_headlines(title_list)
    return {"sentiments": sentiment_map}


@router.post("/analyze")
def analyze_news(payload: NewsPayload):
    """
    Full structured signal analysis (Gemini) — ticker, sector, impact score, reasoning.
    """
    signals = analyze_indian_market_news(payload.text)
    return {"signals": signals}


voice_sessions = {}

@router.post("/voice")
async def ask_voice_bot(payload: VoicePayload):
    """
    Interacts with the CFA Assistant over text and/or voice.
    - Voice input (audio_in_base64) is transcribed via Google Cloud Speech-to-Text.
    - If mode == "VOICE", the reply is also synthesized to audio via Google
      Cloud Text-to-Speech and returned as base64-encoded MP3.

    Async so this awaits straight through to the orchestrator on the app's
    own persistent event loop, instead of the previous sync route handing off
    to a per-call asyncio.run() — that pattern broke the shared genai.Client's
    async transport after the first request in each process ("Event loop is
    closed"), which was silently mis-routing and dropping replies.
    """
    if not payload.prompt and not payload.audio_in_base64:
        raise HTTPException(status_code=400, detail="Provide either 'prompt' text or 'audio_in_base64'.")

    try:
        key = f"{payload.session_id}_{payload.persona}"
        if key not in voice_sessions:
            voice_sessions[key] = CFAMultiAgentBot(voice_persona=payload.persona)

        assistant = voice_sessions[key]
        audio_bytes_in = base64.b64decode(payload.audio_in_base64) if payload.audio_in_base64 else None

        audio_bytes_out, text_resp, transcript, route_meta = await assistant.process_query(
            user_prompt=payload.prompt,
            audio_bytes=audio_bytes_in,
            mode=payload.mode,
            location=payload.location,
            history=[t.model_dump() for t in (payload.history or [])],
        )

        audio_b64 = base64.b64encode(audio_bytes_out).decode("utf-8") if audio_bytes_out else None

        return {"audio_base64": audio_b64, "text": text_resp, "transcript": transcript, "route": route_meta}
    except Exception as e:
        print(f"Voice Bot Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Voice generation failed: {type(e).__name__}: {e}")


@router.post("/trade")
def execute_trade(payload: TradePayload, uid: str = Depends(get_current_user_uid)):
    """
    Executes a trade order via SmartOrderRouter. Requires Firebase Auth token.
    """
    config       = get_broker_config(uid)
    broker_type  = config.get("active_broker", "PAPER_TRADING")
    enc_token    = config.get("encrypted_broker_token", "")

    router_engine = SmartOrderRouter(broker_type=broker_type, encrypted_broker_token=enc_token)
    try:
        receipt  = router_engine.route_order(
            ticker=payload.ticker, quantity=payload.quantity,
            price=payload.price, current_market_price=payload.current_market_price
        )
        trade_id = save_trade_execution(uid, receipt)
        return {"status": "SUCCESS", "trade_id": trade_id, "receipt": receipt}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Execution failed.")