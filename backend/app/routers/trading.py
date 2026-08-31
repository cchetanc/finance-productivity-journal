import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from ..auth import get_current_user_uid
from ..database import db, list_algo_executions, save_trade_execution
from ..trading.algos import AlgoParams, AlgoType
from ..trading.angel_one_client import AngelOneClient
from ..trading.broker_base import BrokerError, OrderRequest, OrderSide, OrderType
from ..trading.engine import ExecutionEngine, RiskLimits
from ..trading.live_config import MissingCredentialError, get_angel_one_live_credentials
from ..trading.paper_broker import PaperBrokerClient

logger = logging.getLogger("trading.router")
router = APIRouter(prefix="/api/trading", tags=["Trading"])

# Paper trading needs no credentials, so it's always available.
_paper_engine = ExecutionEngine(broker=PaperBrokerClient())

# The live engine wraps a single Angel One account, sourced entirely from
# Secret Manager (see trading/live_config.py) — never from a request body.
# Built lazily on first LIVE use and cached for the process's lifetime.
# In-memory execution state (see engine.py docstring) means this is fine for
# a single Cloud Run instance; for horizontal scaling, back it with the
# durable-queue approach noted there.
_live_engine: ExecutionEngine | None = None


def _persist_execution(execution):
    ref = (
        db.collection("users").document(execution.uid)
        .collection("algo_executions").document(execution.execution_id)
    )
    ref.set(execution.to_dict(), merge=True)


async def _get_live_engine() -> ExecutionEngine:
    global _live_engine
    if _live_engine:
        return _live_engine

    try:
        creds = get_angel_one_live_credentials()
    except MissingCredentialError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    client = AngelOneClient(
        api_key=creds.api_key, client_code=creds.client_code, pin=creds.pin, totp_secret=creds.totp_secret
    )
    try:
        await client.connect()
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # TODO: replace with real per-account limits once you've decided them —
    # these defaults are deliberately conservative for a freshly-wired live account.
    _live_engine = ExecutionEngine(
        broker=client,
        risk_limits=RiskLimits(max_order_value=50_000.0, max_total_value=200_000.0),
        persist_fn=_persist_execution,
    )
    return _live_engine


# ---------------------------------------------------------------------------
# Broker status
# ---------------------------------------------------------------------------

@router.get("/broker/status")
async def broker_status():
    """Reports whether live credentials are configured and reachable —
    never returns any credential value, only booleans/metadata."""
    from ..trading import live_config

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
    if body.order_type == OrderType.LIMIT and body.limit_price is None:
        raise HTTPException(status_code=400, detail="limit_price is required for LIMIT orders")

    engine = await _get_live_engine() if body.mode == "LIVE" else _paper_engine
    order = OrderRequest(
        symbol=body.symbol, exchange=body.exchange, side=body.side,
        quantity=body.quantity, order_type=body.order_type, limit_price=body.limit_price,
    )
    try:
        result = await engine.broker.place_order(order)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    trade_id = save_trade_execution(uid, {
        "symbol": body.symbol, "side": body.side.value, "quantity": body.quantity,
        "order_type": body.order_type.value, "mode": body.mode,
        "broker_order_id": result.broker_order_id, "status": result.status.value,
    })
    return {"trade_id": trade_id, "broker_order_id": result.broker_order_id, "status": result.status.value}


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
    engine = await _get_live_engine() if body.mode == "LIVE" else _paper_engine
    params = _params_from_request(body)
    try:
        execution = await engine.start(uid, body.algo_type, params)
    except Exception as e:  # noqa: BLE001 - includes RiskLimitError
        raise HTTPException(status_code=400, detail=str(e)) from e
    return execution.to_dict()


@router.get("/algo/{execution_id}")
async def get_algo_status(execution_id: str, mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    engine = _live_engine if mode == "LIVE" else _paper_engine
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
    engine = _live_engine if mode == "LIVE" else _paper_engine
    if not engine or not engine.stop(execution_id):
        raise HTTPException(status_code=404, detail="Execution not found or already finished")
    return {"message": "Stop requested."}


@router.get("/algo")
def get_algo_history(uid: str = Depends(get_current_user_uid)):
    return list_algo_executions(uid)


@router.get("/positions")
async def get_positions(mode: str = "PAPER", uid: str = Depends(get_current_user_uid)):
    engine = await _get_live_engine() if mode == "LIVE" else _paper_engine
    try:
        return await engine.broker.get_positions()
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e