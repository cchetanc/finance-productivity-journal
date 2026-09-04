"""
Simulated broker: fills orders instantly at the quoted price (or the limit
price, if better). No network calls, no real money. Used as the default
broker for any algo execution that isn't explicitly started with mode="LIVE",
and useful for unit-testing strategies without touching Angel One at all.
"""
import random
import uuid
from datetime import datetime

from .broker_base import (
    BrokerClient,
    Funds,
    InsufficientFundsError,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)

# Starting virtual cash for every paper-trading session — arbitrary but
# realistic enough (₹10 lakh) for position sizing to feel meaningful in the
# UI without being a real balance.
_STARTING_PAPER_CASH = 1_000_000.0


class PaperBrokerClient(BrokerClient):
    name = "PAPER_TRADING"

    def __init__(self, starting_prices: dict[str, float] | None = None, starting_cash: float = _STARTING_PAPER_CASH):
        # symbol -> last price, so repeated get_quote calls drift slightly
        # instead of being perfectly flat (more realistic for testing TWAP/VWAP).
        self._prices = dict(starting_prices or {})
        self._orders: dict[str, OrderResult] = {}
        self._cash = starting_cash

    def _price_for(self, symbol: str) -> float:
        base = self._prices.setdefault(symbol, 1000.0)
        drift = base * random.uniform(-0.001, 0.001)
        new_price = round(base + drift, 2)
        self._prices[symbol] = new_price
        return new_price

    async def get_quote(self, symbol: str, exchange: str) -> Quote:
        return Quote(symbol=symbol, ltp=self._price_for(symbol), volume=random.randint(10_000, 500_000))

    async def get_funds(self) -> Funds:
        return Funds(available_cash=round(self._cash, 2), net=round(self._cash, 2))

    async def place_order(self, order: OrderRequest) -> OrderResult:
        ltp = self._price_for(order.symbol)
        if order.order_type == OrderType.LIMIT:
            crossable = (
                (order.side == OrderSide.BUY and order.limit_price >= ltp)
                or (order.side == OrderSide.SELL and order.limit_price <= ltp)
            )
            fill_price = order.limit_price if crossable else None
        else:
            fill_price = ltp

        # Paper wallet still enforces cash on BUY fills, same as a real broker would —
        # so a PAPER run gives an honest preview of whether an equivalent LIVE order
        # would be blocked for insufficient balance too.
        if fill_price and order.side == OrderSide.BUY:
            cost = fill_price * order.quantity
            if cost > self._cash:
                raise InsufficientFundsError(required=cost, available=self._cash)
            self._cash -= cost
        elif fill_price and order.side == OrderSide.SELL:
            self._cash += fill_price * order.quantity

        order_id = f"PAPER-{uuid.uuid4().hex[:10]}"
        result = OrderResult(
            broker_order_id=order_id,
            status=OrderStatus.FILLED if fill_price else OrderStatus.OPEN,
            filled_quantity=order.quantity if fill_price else 0,
            average_price=fill_price,
            placed_at=datetime.utcnow(),
            raw={"symbol": order.symbol, "side": order.side.value, "qty": order.quantity},
        )
        self._orders[order_id] = result
        return result

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        result = self._orders.get(broker_order_id)
        if result:
            result.status = OrderStatus.CANCELLED
            return result
        return OrderResult(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED)

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        return self._orders.get(broker_order_id) or OrderResult(
            broker_order_id=broker_order_id, status=OrderStatus.REJECTED, error_message="unknown order"
        )

    async def get_positions(self) -> list:
        return [
            {"symbol": r.raw.get("symbol"), "quantity": r.filled_quantity, "avg_price": r.average_price}
            for r in self._orders.values()
            if r.status == OrderStatus.FILLED
        ]