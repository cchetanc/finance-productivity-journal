"""
Shared trading-engine access.

Both routers/trading.py (the REST API the Streamlit Trade Terminal calls)
and tools_impls.py (the "quant analyst" agent's place_trade_order tool) need
the exact same thing: the right ExecutionEngine for a given uid+mode, with
the same risk limits and the same PAPER-engine singleton. This used to live
only in routers/trading.py; it's factored out here so the agent tool and the
REST endpoint can never drift into two different code paths for something as
risk-sensitive as "which engine actually places this order."
"""
import logging
from typing import Optional

from ..database import db, save_trade_execution
from .angel_one_client import AngelOneClient
from .broker_base import BrokerError, InsufficientFundsError, OrderRequest, OrderSide, OrderStatus, OrderType
from .credentials import get_angel_one_credentials
from .engine import ExecutionEngine, RiskLimits
from .paper_broker import PaperBrokerClient

logger = logging.getLogger("trading.service")


def _persist_execution(execution):
    ref = (
        db.collection("users").document(execution.uid)
        .collection("algo_executions").document(execution.execution_id)
    )
    ref.set(execution.to_dict(), merge=True)


# Process-wide singletons — see routers/trading.py's original docstring for
# why this is safe: all state is passed in/out and tagged with uid, so
# sharing the engine instances across requests doesn't leak between users.
paper_engine = ExecutionEngine(broker=PaperBrokerClient(), persist_fn=_persist_execution)
_live_engines: dict[str, ExecutionEngine] = {}


async def get_live_engine(uid: str) -> ExecutionEngine:
    if uid in _live_engines:
        return _live_engines[uid]

    creds = get_angel_one_credentials(uid)
    if not creds:
        raise BrokerError("Broker credentials not configured for this user.")

    client = AngelOneClient(
        api_key=creds.api_key, client_code=creds.client_code, pin=creds.pin, totp_secret=creds.totp_secret
    )
    await client.connect()

    # TODO: replace with real per-account limits once you've decided them —
    # these defaults are deliberately conservative for a freshly-wired live account.
    engine = ExecutionEngine(
        broker=client,
        risk_limits=RiskLimits(max_order_value=50_000.0, max_total_value=200_000.0),
        persist_fn=_persist_execution,
    )
    _live_engines[uid] = engine
    return engine


def invalidate_live_engine(uid: str):
    """Called after credentials are updated so the next order picks up the new ones."""
    _live_engines.pop(uid, None)


async def get_engine(uid: str, mode: str) -> ExecutionEngine:
    return await get_live_engine(uid) if mode == "LIVE" else paper_engine


async def place_simple_order(
    uid: str, symbol: str, exchange: str, side: OrderSide, quantity: int,
    order_type: OrderType = OrderType.MARKET, limit_price: Optional[float] = None,
    mode: str = "PAPER",
) -> dict:
    """Places one plain (non-algo) order and persists the trade record —
    the single code path both POST /api/trading/orders and the agent's
    place_trade_order tool call into, so a trade placed from chat is
    recorded and risk-checked identically to one placed from the terminal.

    Returns a plain dict (never raises for the ordinary "can't afford it"
    case) so callers — especially the agent tool, which needs to relay this
    straight back to the user in plain language — don't need to know
    exception types: {"ok": bool, ...}.
    """
    if order_type == OrderType.LIMIT and limit_price is None:
        return {"ok": False, "error": "limit_price is required for LIMIT orders."}

    try:
        engine = await get_engine(uid, mode)
    except BrokerError as e:
        return {"ok": False, "error": str(e)}

    order = OrderRequest(
        symbol=symbol, exchange=exchange, side=side, quantity=quantity,
        order_type=order_type, limit_price=limit_price,
    )

    # Pre-trade funds check: we now just log warnings if it fails,
    # rather than blocking the trade, per user request.
    if side == OrderSide.BUY:
        try:
            quote = await engine.broker.get_quote(symbol, exchange)
            est_price = limit_price or quote.ltp
            await engine.check_funds(est_price, quantity)
        except InsufficientFundsError as e:
            logger.warning("Pre-trade funds check flagged insufficient balance: %s. Proceeding to place trade anyway.", e)
        except BrokerError as e:
            if mode == "LIVE":
                logger.warning("Pre-trade funds check failed (%s) — reconnecting once for uid=%s", e, uid)
                invalidate_live_engine(uid)
                try:
                    engine = await get_engine(uid, mode)
                    quote = await engine.broker.get_quote(symbol, exchange)
                    est_price = limit_price or quote.ltp
                    await engine.check_funds(est_price, quantity)
                except InsufficientFundsError as e2:
                    logger.warning("Pre-trade funds check flagged insufficient balance after reconnect: %s. Proceeding anyway.", e2)
                except BrokerError as e2:
                    logger.warning("Pre-trade funds check failed again (%s). Proceeding anyway.", e2)
            else:
                logger.warning("Pre-trade funds check failed (%s). Proceeding anyway.", e)

    try:
        result = await engine.broker.place_order(order)
    except InsufficientFundsError as e:
        return {
            "ok": False, "insufficient_funds": True,
            "required": round(e.required, 2), "available": round(e.available, 2),
            "error": str(e),
        }
    except BrokerError as e:
        return {"ok": False, "error": str(e)}

    trade_id = save_trade_execution(uid, {
        "symbol": symbol, "exchange": exchange, "side": side.value,
        "quantity": quantity, "order_type": order_type.value,
        "limit_price": limit_price, "mode": mode,
        "broker_order_id": result.broker_order_id, "status": result.status.value,
    })
    return {
        "ok": True, "trade_id": trade_id, "broker_order_id": result.broker_order_id,
        "status": result.status.value, "average_price": result.average_price,
        "filled_quantity": result.filled_quantity,
    }


async def get_funds_dict(uid: str, mode: str = "PAPER") -> dict:
    """Wallet balance for the Trade Terminal's header and for the agent's
    pre-trade balance check. Returns an {"ok": False, "error": ...} shape
    (rather than raising) so the frontend can render a clear inline message
    instead of a broken widget when live credentials aren't configured yet."""
    try:
        engine = await get_engine(uid, mode)
        try:
            funds = await engine.broker.get_funds()
        except BrokerError as e:
            if mode != "LIVE":
                raise
            # The cached live session may have gone stale server-side (Angel
            # One doesn't tell us when — it just starts failing RMS/funds
            # calls with a generic error). Drop it and reconnect once before
            # giving up, instead of returning the same dead-session error on
            # every request until the Cloud Run instance recycles.
            logger.warning("Live funds fetch failed (%s) — reconnecting once for uid=%s", e, uid)
            invalidate_live_engine(uid)
            engine = await get_engine(uid, mode)
            funds = await engine.broker.get_funds()
    except BrokerError as e:
        return {"ok": False, "error": str(e), "mode": mode}
    return {
        "ok": True, "mode": mode, "broker": engine.broker.name,
        "available_cash": funds.available_cash, "net": funds.net,
        "utilised_debits": funds.utilised_debits, "collateral": funds.collateral,
    }