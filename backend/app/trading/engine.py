"""
Execution engine.

Drives a Strategy against a BrokerClient: pulls each child OrderRequest,
runs it through risk checks, sends it to the broker, records the fill, and
persists running state to Firestore so /algo/{id}/status can be polled.

Concurrency note: this runs the algo as an asyncio task inside the current
process. That's fine for a single Cloud Run instance handling its own
requests, but an algo that's mid-execution when the instance recycles will
be lost. For anything beyond testing, move `_execute` onto a durable queue
(Cloud Tasks / Pub-Sub + Cloud Run job) keyed by execution_id so it survives
restarts. The Firestore state model here is already shaped for that.
"""
import asyncio
import logging
import uuid
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Optional

from .algos import AlgoParams, AlgoType, build_strategy
from .broker_base import BrokerClient, BrokerError, OrderResult, OrderSide, OrderStatus

logger = logging.getLogger("trading.engine")


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"       # momentum sniper: watch window closed with no trigger


class RiskLimitError(Exception):
    pass


class RiskLimits:
    """Per-user guardrails applied to every child order before it's sent to
    the broker. Values should come from the user's saved risk config
    (Firestore), with conservative defaults if unset."""

    def __init__(self, max_order_value: float = 100_000.0, max_total_value: float = 500_000.0,
                 kill_switch_engaged: bool = False):
        self.max_order_value = max_order_value
        self.max_total_value = max_total_value
        self.kill_switch_engaged = kill_switch_engaged

    def check_child_order(self, estimated_price: float, quantity: int):
        if self.kill_switch_engaged:
            raise RiskLimitError("Trading kill switch is engaged for this account.")
        value = estimated_price * quantity
        if value > self.max_order_value:
            raise RiskLimitError(
                f"Child order value {value:,.2f} exceeds max_order_value {self.max_order_value:,.2f}"
            )

    def check_total_order(self, estimated_price: float, total_quantity: int):
        value = estimated_price * total_quantity
        if value > self.max_total_value:
            raise RiskLimitError(
                f"Total order value {value:,.2f} exceeds max_total_value {self.max_total_value:,.2f}"
            )


class AlgoExecution:
    """Runtime + persisted state for a single algo run."""

    def __init__(self, execution_id: str, uid: str, algo_type: AlgoType, params: AlgoParams):
        self.execution_id = execution_id
        self.uid = uid
        self.algo_type = algo_type
        self.params = params
        self.status = ExecutionStatus.PENDING
        self.child_orders: list[OrderResult] = []
        self.error_message: Optional[str] = None
        self.created_at = datetime.utcnow()
        self.updated_at = self.created_at
        self._cancel_requested = False

    def request_stop(self):
        self._cancel_requested = True

    def total_filled(self) -> int:
        return sum(o.filled_quantity for o in self.child_orders if o.status == OrderStatus.FILLED)

    def average_fill_price(self) -> Optional[float]:
        filled = [o for o in self.child_orders if o.status == OrderStatus.FILLED and o.average_price]
        if not filled:
            return None
        total_qty = sum(o.filled_quantity for o in filled)
        if total_qty == 0:
            return None
        weighted = sum(o.average_price * o.filled_quantity for o in filled)
        return round(weighted / total_qty, 4)

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "uid": self.uid,
            "algo_type": self.algo_type.value,
            "symbol": self.params.symbol,
            "exchange": self.params.exchange,
            "side": self.params.side.value,
            "total_quantity": self.params.total_quantity,
            "status": self.status.value,
            "total_filled": self.total_filled(),
            "average_fill_price": self.average_fill_price(),
            "child_order_count": len(self.child_orders),
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ExecutionEngine:
    def __init__(self, broker: BrokerClient, risk_limits: Optional[RiskLimits] = None,
                 persist_fn=None):
        """
        broker: the BrokerClient (Angel One live, or Paper) this engine trades through.
        risk_limits: guardrails checked before every child order.
        persist_fn: optional callable(AlgoExecution) -> None, invoked after every
            state change, to write through to Firestore. Kept pluggable so this
            module has no direct Firestore dependency.
        """
        self.broker = broker
        self.risk_limits = risk_limits or RiskLimits()
        self._persist_fn = persist_fn
        self._executions: dict[str, AlgoExecution] = {}

    def _persist(self, execution: AlgoExecution):
        execution.updated_at = datetime.utcnow()
        if self._persist_fn:
            try:
                self._persist_fn(execution)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to persist execution %s", execution.execution_id)

    def get(self, execution_id: str) -> Optional[AlgoExecution]:
        return self._executions.get(execution_id)

    def stop(self, execution_id: str) -> bool:
        execution = self._executions.get(execution_id)
        if not execution:
            return False
        execution.request_stop()
        return True

    async def start(self, uid: str, algo_type: AlgoType, params: AlgoParams) -> AlgoExecution:
        execution_id = str(uuid.uuid4())
        execution = AlgoExecution(execution_id, uid, algo_type, params)
        self._executions[execution_id] = execution
        self._persist(execution)

        # Pre-trade total-size risk check against current LTP, before we
        # commit to running anything.
        quote = await self.broker.get_quote(params.symbol, params.exchange)
        try:
            self.risk_limits.check_total_order(quote.ltp, params.total_quantity)
        except RiskLimitError as e:
            execution.status = ExecutionStatus.FAILED
            execution.error_message = str(e)
            self._persist(execution)
            raise

        asyncio.create_task(self._run(execution))
        return execution

    async def _run(self, execution: AlgoExecution):
        execution.status = ExecutionStatus.RUNNING
        self._persist(execution)

        strategy = build_strategy(execution.algo_type, execution.params, self.broker)
        try:
            async for child_order in strategy.run():
                if execution._cancel_requested:
                    execution.status = ExecutionStatus.STOPPED
                    self._persist(execution)
                    return

                # Risk check per child order, priced at current LTP for market orders.
                quote = await self.broker.get_quote(child_order.symbol, child_order.exchange)
                est_price = child_order.limit_price or quote.ltp
                self.risk_limits.check_child_order(est_price, child_order.quantity)

                try:
                    result = await self.broker.place_order(child_order)
                except BrokerError as e:
                    logger.error("Child order failed for execution %s: %s", execution.execution_id, e)
                    result = OrderResult(
                        broker_order_id="", status=OrderStatus.REJECTED, error_message=str(e)
                    )

                execution.child_orders.append(result)
                self._persist(execution)

            if not execution.child_orders and execution.algo_type == AlgoType.MOMENTUM_SNIPER:
                execution.status = ExecutionStatus.EXPIRED
            else:
                execution.status = ExecutionStatus.COMPLETED
        except RiskLimitError as e:
            execution.status = ExecutionStatus.FAILED
            execution.error_message = f"Risk limit breached, execution halted: {e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("Execution %s failed", execution.execution_id)
            execution.status = ExecutionStatus.FAILED
            execution.error_message = str(e)
        finally:
            self._persist(execution)