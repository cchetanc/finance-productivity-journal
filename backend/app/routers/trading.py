import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from ..auth import get_current_user_uid
from ..database import list_algo_executions, list_trades
from ..trading import service as trading_service
from ..trading.algos import AlgoParams, AlgoType
from ..trading.broker_base import BrokerError, OrderSide, OrderType
from ..trading.credentials import get_angel_one_credentials, save_angel_one_credentials, AngelOneCredentials

logger = logging.getLogger("trading.router")
router = APIRouter(prefix="/api/trading", tags=["Trading"])


class AngelOneConfigPayload(BaseModel):
    api_key: str
    client_code: str
    pin: str
    totp_secret: str

@router.get("/config/angel-one")
def get_angel_one_config(uid: str = Depends(get_current_user_uid)):
    creds = get_angel_one_credentials(uid)
    return {"configured": creds is not None}

@router.post("/config/angel-one")
def update_angel_one_config(payload: AngelOneConfigPayload, uid: str = Depends(get_current_user_uid)):
    creds = AngelOneCredentials(
        api_key=payload.api_key,
        client_code=payload.client_code,
        pin=payload.pin,
        totp_secret=payload.totp_secret,
    )
    save_angel_one_credentials(uid, creds)
    # Clear any existing cached engine so the new credentials take effect
    trading_service.invalidate_live_engine(uid)
    return {"status": "ok", "message": "Credentials updated successfully."}


# ---------------------------------------------------------------------------
# Broker status
# ---------------------------------------------------------------------------

@router.get("/broker/status")
async def broker_status():
    """Reports whether live credentials are configured and reachable —
    never returns any credential value, only booleans/metadata."""
    from ..trading import live_config
    from ..trading.live_config import get_angel_one_live_credentials, MissingCredentialError

    configured = {
        field: bool(live_config._secret_id_for(field)) for field in live_config._ENV_DEFAULTS
    }
    try:
        get_angel_one_live_credentials()
        reachable = True
        detail = None
    except MissingCredentialError as e:
        reachable = False
        detail = str(e)
    return {"secret_ids_configured": configured, "secret_manager_reachable": reachable, "detail": detail}


# ---------------------------------------------------------------------------
# Wallet / funds
# ---------------------------------------------------------------------------

@router.get("/funds")
async def get_funds(mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    """Available balance in the active wallet — the PAPER simulator's virtual
    cash, or the real Angel One account's available margin (via SmartAPI's
    rmsLimit()) when mode=LIVE. Used by the Trade Terminal's balance header
    and by the agent's pre-trade insufficient-funds check."""
    return await trading_service.get_funds_dict(uid, mode=mode)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

class PlaceOrderRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[float] = None
    mode: str = Field("PAPER", description="'LIVE' or 'PAPER'")


@router.post("/orders")
async def place_order(body: PlaceOrderRequest, uid: str = Depends(get_current_user_uid)):
    result = await trading_service.place_simple_order(
        uid, symbol=body.symbol, exchange=body.exchange, side=body.side,
        quantity=body.quantity, order_type=body.order_type, limit_price=body.limit_price,
        mode=body.mode,
    )
    if not result.get("ok"):
        status_code = 400 if result.get("insufficient_funds") else 502
        raise HTTPException(status_code=status_code, detail=result.get("error"))
    return {"trade_id": result["trade_id"], "broker_order_id": result["broker_order_id"], "status": result["status"]}


# ---------------------------------------------------------------------------
# Algo executions
# ---------------------------------------------------------------------------

class StartAlgoRequest(BaseModel):
    algo_type: AlgoType
    symbol: str
    exchange: str = "NSE"
    side: OrderSide
    total_quantity: int
    mode: str = Field("PAPER", description="'LIVE' or 'PAPER' — defaults to PAPER for safety")

    clip_size: Optional[int] = None
    price_limit: Optional[float] = None
    duration_minutes: Optional[int] = None
    slice_count: Optional[int] = None
    breakout_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    watch_timeout_minutes: int = 30
    randomize_timing: bool = True


def _params_from_request(body: StartAlgoRequest) -> AlgoParams:
    return AlgoParams(
        symbol=body.symbol, exchange=body.exchange, side=body.side, total_quantity=body.total_quantity,
        clip_size=body.clip_size, price_limit=body.price_limit,
        duration_minutes=body.duration_minutes, slice_count=body.slice_count,
        breakout_price=body.breakout_price, stop_loss_price=body.stop_loss_price,
        watch_timeout_minutes=body.watch_timeout_minutes, randomize_timing=body.randomize_timing,
    )


@router.post("/algo/preview")
def preview_algo(body: StartAlgoRequest):
    """Returns the planned child-order schedule without placing anything —
    lets the UI show the user what an algo will actually do before they
    confirm it."""
    from ..trading.algos import build_strategy

    params = _params_from_request(body)
    strategy = build_strategy(body.algo_type, params, broker=None)
    return {"algo_type": body.algo_type.value, "plan": strategy.plan()}


@router.post("/algo/start")
async def start_algo(body: StartAlgoRequest, uid: str = Depends(get_current_user_uid)):
    engine = await trading_service.get_engine(uid, body.mode)
    params = _params_from_request(body)
    try:
        execution = await engine.start(uid, body.algo_type, params, mode=body.mode)
    except Exception as e:  # noqa: BLE001 - includes RiskLimitError / InsufficientFundsError
        raise HTTPException(status_code=400, detail=str(e)) from e
    return execution.to_dict()


@router.get("/algo/{execution_id}")
async def get_algo_status(execution_id: str, mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    from ..database import db
    engine = trading_service._live_engines.get(uid) if mode == "LIVE" else trading_service.paper_engine
    execution = engine.get(execution_id) if engine else None
    if not execution:
        # Fall back to Firestore in case this process didn't run it (e.g.
        # after an instance restart) — see engine.py's durability note.
        doc = db.collection("users").document(uid).collection("algo_executions").document(execution_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Execution not found")
        return doc.to_dict()
    return execution.to_dict()


@router.post("/algo/{execution_id}/stop")
async def stop_algo(execution_id: str, mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    engine = trading_service._live_engines.get(uid) if mode == "LIVE" else trading_service.paper_engine
    if not engine or not engine.stop(execution_id):
        raise HTTPException(status_code=404, detail="Execution not found or already finished")
    return {"message": "Stop requested."}


@router.get("/algo")
def get_algo_history(uid: str = Depends(get_current_user_uid)):
    return list_algo_executions(uid)


@router.get("/orders")
def get_order_history(uid: str = Depends(get_current_user_uid)):
    """Manual (non-algo) order receipts, newest first — combine with
    /api/trading/algo on the frontend to build a full trade log."""
    return list_trades(uid)


@router.get("/positions")
async def get_positions(mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    engine = await trading_service.get_engine(uid, mode)
    try:
        return await engine.broker.get_positions()
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e