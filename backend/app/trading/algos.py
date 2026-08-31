"""
Order-execution algorithms.

Each strategy is an async generator: it yields child OrderRequests one at a
time, and the engine (engine.py) is responsible for actually sending them to
the broker, recording fills, and applying risk checks between yields. This
keeps the strategies themselves broker-agnostic and easy to unit test.

None of these "guarantee profit" — they're execution algorithms, i.e. they
control *how* a given quantity gets bought or sold (to reduce market impact
or chase a signal), not *whether* the trade is a good idea. Sizing / entry
decisions still come from you or from upstream signals.
"""
import asyncio
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import AsyncIterator, Optional

from .broker_base import BrokerClient, OrderRequest, OrderSide, OrderType


class AlgoType(str, Enum):
    LIMIT = "LIMIT"                 # single order at a fixed price, no slicing
    ICEBERG = "ICEBERG"             # large order shown to the market in small visible clips
    TWAP = "TWAP"                   # equal-sized slices spread evenly over a time window
    VWAP = "VWAP"                   # slices weighted to track the market's historical volume curve
    MOMENTUM_SNIPER = "MOMENTUM_SNIPER"  # waits for a breakout/move, then fires fast with a stop


@dataclass
class AlgoParams:
    symbol: str
    exchange: str
    side: OrderSide
    total_quantity: int

    # Iceberg
    clip_size: Optional[int] = None            # visible slice size per child order
    price_limit: Optional[float] = None         # cap/floor price the iceberg won't cross

    # TWAP / VWAP
    duration_minutes: Optional[int] = None
    slice_count: Optional[int] = None            # defaults to one slice/minute if unset

    # Momentum sniper
    breakout_price: Optional[float] = None       # trigger: buy stops above / sell stops below this
    stop_loss_price: Optional[float] = None       # protective stop once filled
    watch_timeout_minutes: int = 30               # give up waiting for the breakout after this long

    # shared
    randomize_timing: bool = True                 # jitter between clips to avoid a predictable footprint


class Strategy(ABC):
    algo_type: AlgoType

    def __init__(self, params: AlgoParams, broker: BrokerClient, clock_sleep=asyncio.sleep):
        self.params = params
        self.broker = broker
        self._sleep = clock_sleep  # injectable for tests

    @abstractmethod
    def plan(self) -> list[dict]:
        """Returns a human-readable list of planned child orders/steps, used
        to show a preview to the user before the algo is confirmed to run."""

    @abstractmethod
    async def run(self) -> AsyncIterator[OrderRequest]:
        """Yields child OrderRequests in sequence. The engine sends each one
        to the broker and drives the pacing between yields via this
        strategy's own internal sleeps."""


class LimitStrategy(Strategy):
    """Not really an 'algo' — a single limit order. Included so the engine
    has one code path for both plain limit orders and algo orders."""

    algo_type = AlgoType.LIMIT

    def plan(self) -> list[dict]:
        return [{
            "step": 1,
            "quantity": self.params.total_quantity,
            "order_type": "LIMIT",
            "price": self.params.price_limit,
        }]

    async def run(self) -> AsyncIterator[OrderRequest]:
        yield OrderRequest(
            symbol=self.params.symbol,
            exchange=self.params.exchange,
            side=self.params.side,
            quantity=self.params.total_quantity,
            order_type=OrderType.LIMIT,
            limit_price=self.params.price_limit,
        )


class IcebergStrategy(Strategy):
    """Splits a large order into repeated small 'clips' so only a fraction of
    the total size is ever visible on the order book at once, reducing the
    signal a large resting order gives away. Each clip is re-placed as the
    previous one fills, at (or better than) the configured price limit."""

    algo_type = AlgoType.ICEBERG

    def _clip_sizes(self) -> list[int]:
        clip = self.params.clip_size or max(1, self.params.total_quantity // 10)
        full_clips, remainder = divmod(self.params.total_quantity, clip)
        sizes = [clip] * full_clips
        if remainder:
            sizes.append(remainder)
        return sizes

    def plan(self) -> list[dict]:
        return [
            {"step": i + 1, "quantity": q, "order_type": "LIMIT", "price": self.params.price_limit}
            for i, q in enumerate(self._clip_sizes())
        ]

    async def run(self) -> AsyncIterator[OrderRequest]:
        import random

        for i, qty in enumerate(self._clip_sizes()):
            yield OrderRequest(
                symbol=self.params.symbol,
                exchange=self.params.exchange,
                side=self.params.side,
                quantity=qty,
                order_type=OrderType.LIMIT,
                limit_price=self.params.price_limit,
                client_order_tag=f"ICEBERG-{i}",
            )
            if self.params.randomize_timing:
                await self._sleep(random.uniform(2, 8))
            else:
                await self._sleep(2)


class TWAPStrategy(Strategy):
    """Time-Weighted Average Price: splits the order into equal slices spread
    evenly across a fixed time window, so execution roughly tracks the
    average price over that window rather than one single moment."""

    algo_type = AlgoType.TWAP

    def _slices(self) -> list[int]:
        n = self.params.slice_count or max(1, self.params.duration_minutes or 1)
        base, remainder = divmod(self.params.total_quantity, n)
        sizes = [base] * n
        for i in range(remainder):
            sizes[i] += 1
        return [s for s in sizes if s > 0]

    def _interval_seconds(self) -> float:
        n = len(self._slices())
        total_seconds = (self.params.duration_minutes or 1) * 60
        return total_seconds / max(1, n)

    def plan(self) -> list[dict]:
        interval = self._interval_seconds()
        return [
            {"step": i + 1, "quantity": q, "order_type": "MARKET", "at_offset_seconds": round(i * interval)}
            for i, q in enumerate(self._slices())
        ]

    async def run(self) -> AsyncIterator[OrderRequest]:
        import random

        interval = self._interval_seconds()
        for i, qty in enumerate(self._slices()):
            yield OrderRequest(
                symbol=self.params.symbol,
                exchange=self.params.exchange,
                side=self.params.side,
                quantity=qty,
                order_type=OrderType.MARKET,
                client_order_tag=f"TWAP-{i}",
            )
            if i < len(self._slices()) - 1:
                jitter = random.uniform(-0.15, 0.15) * interval if self.params.randomize_timing else 0
                await self._sleep(max(0.0, interval + jitter))


class VWAPStrategy(Strategy):
    """Volume-Weighted Average Price: slices the order proportionally to a
    historical intraday volume curve, so execution weight is concentrated in
    the periods the market typically trades most (open/close), rather than
    spread flat like TWAP. Falls back to a generic U-shaped curve if no
    historical profile is supplied."""

    algo_type = AlgoType.VWAP

    # Generic NSE-style U-shaped intraday volume profile (10 buckets,
    # fractions sum to 1.0). Replace with a real historical profile fetched
    # per-symbol for meaningfully better tracking.
    _DEFAULT_VOLUME_CURVE = [0.16, 0.12, 0.09, 0.07, 0.06, 0.06, 0.07, 0.09, 0.12, 0.16]

    def __init__(self, params: AlgoParams, broker: BrokerClient, clock_sleep=asyncio.sleep,
                 volume_curve: Optional[list[float]] = None):
        super().__init__(params, broker, clock_sleep)
        self.volume_curve = volume_curve or self._DEFAULT_VOLUME_CURVE

    def _slices(self) -> list[int]:
        remaining = self.params.total_quantity
        sizes = []
        for frac in self.volume_curve[:-1]:
            q = round(self.params.total_quantity * frac)
            sizes.append(q)
            remaining -= q
        sizes.append(max(0, remaining))  # last bucket absorbs rounding
        return [s for s in sizes if s > 0]

    def _interval_seconds(self) -> float:
        n = len(self.volume_curve)
        total_seconds = (self.params.duration_minutes or len(self.volume_curve)) * 60
        return total_seconds / n

    def plan(self) -> list[dict]:
        interval = self._interval_seconds()
        return [
            {"step": i + 1, "quantity": q, "order_type": "MARKET",
             "volume_weight": self.volume_curve[i], "at_offset_seconds": round(i * interval)}
            for i, q in enumerate(self._slices())
        ]

    async def run(self) -> AsyncIterator[OrderRequest]:
        interval = self._interval_seconds()
        slices = self._slices()
        for i, qty in enumerate(slices):
            yield OrderRequest(
                symbol=self.params.symbol,
                exchange=self.params.exchange,
                side=self.params.side,
                quantity=qty,
                order_type=OrderType.MARKET,
                client_order_tag=f"VWAP-{i}",
            )
            if i < len(slices) - 1:
                await self._sleep(interval)


class MomentumSniperStrategy(Strategy):
    """Watches price and waits for a breakout trigger (price crossing
    `breakout_price`), then fires the *entire* quantity as a single fast
    market order to capture the move, and immediately arms a protective
    stop-loss child order. If the trigger doesn't happen within
    `watch_timeout_minutes`, the strategy gives up without trading."""

    algo_type = AlgoType.MOMENTUM_SNIPER

    def plan(self) -> list[dict]:
        return [
            {"step": 1, "action": "WATCH", "trigger_price": self.params.breakout_price,
             "timeout_minutes": self.params.watch_timeout_minutes},
            {"step": 2, "action": "FIRE_MARKET_ORDER", "quantity": self.params.total_quantity},
            {"step": 3, "action": "ARM_STOP_LOSS", "stop_price": self.params.stop_loss_price},
        ]

    async def run(self) -> AsyncIterator[OrderRequest]:
        if self.params.breakout_price is None:
            raise ValueError("MOMENTUM_SNIPER requires breakout_price")

        deadline = datetime.utcnow() + timedelta(minutes=self.params.watch_timeout_minutes)
        poll_seconds = 3

        while datetime.utcnow() < deadline:
            quote = await self.broker.get_quote(self.params.symbol, self.params.exchange)
            triggered = (
                (self.params.side == OrderSide.BUY and quote.ltp >= self.params.breakout_price)
                or (self.params.side == OrderSide.SELL and quote.ltp <= self.params.breakout_price)
            )
            if triggered:
                yield OrderRequest(
                    symbol=self.params.symbol,
                    exchange=self.params.exchange,
                    side=self.params.side,
                    quantity=self.params.total_quantity,
                    order_type=OrderType.MARKET,
                    client_order_tag="SNIPER-ENTRY",
                )
                if self.params.stop_loss_price is not None:
                    opposite = OrderSide.SELL if self.params.side == OrderSide.BUY else OrderSide.BUY
                    yield OrderRequest(
                        symbol=self.params.symbol,
                        exchange=self.params.exchange,
                        side=opposite,
                        quantity=self.params.total_quantity,
                        order_type=OrderType.LIMIT,
                        limit_price=self.params.stop_loss_price,
                        client_order_tag="SNIPER-STOP",
                    )
                return
            await self._sleep(poll_seconds)
        # Timed out without a trigger: yield nothing, engine marks EXPIRED.


_STRATEGY_CLASSES: dict[AlgoType, type[Strategy]] = {
    AlgoType.LIMIT: LimitStrategy,
    AlgoType.ICEBERG: IcebergStrategy,
    AlgoType.TWAP: TWAPStrategy,
    AlgoType.VWAP: VWAPStrategy,
    AlgoType.MOMENTUM_SNIPER: MomentumSniperStrategy,
}


def build_strategy(algo_type: AlgoType, params: AlgoParams, broker: BrokerClient) -> Strategy:
    cls = _STRATEGY_CLASSES.get(algo_type)
    if not cls:
        raise ValueError(f"Unknown algo type: {algo_type}")
    return cls(params, broker)