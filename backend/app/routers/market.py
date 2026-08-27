from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from ..gemini import analyze_indian_market_news, SmartOrderRouter, batch_analyze_headlines
from ..sentiment import batch_classify_headlines
from ..auth import get_current_user_uid
from ..database import get_broker_config, save_trade_execution
from ..market_data import get_live_indices, compute_market_mood, get_live_news, INDEX_GROUPS

router = APIRouter(prefix="/api/market", tags=["Market"])


class NewsPayload(BaseModel):
    text: str


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
