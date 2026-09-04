"""
Broker abstraction layer.

Every broker integration (Angel One live, Paper/simulated, future HDFC, etc.)
implements BrokerClient so the algo engine never talks to a vendor SDK directly.
This keeps vendor quirks out of the strategy code and makes it possible to run
the exact same algo in PAPER mode before flipping it to LIVE.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class OrderRequest:
    symbol: str            # trading symbol, e.g. "RELIANCE-EQ"
    exchange: str           # "NSE" / "BSE"
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Optional[float] = None
    # tag used to correlate child orders back to a parent algo execution
    client_order_tag: Optional[str] = None


@dataclass
class OrderResult:
    broker_order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    average_price: Optional[float] = None
    raw: dict = field(default_factory=dict)
    placed_at: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None


@dataclass
class Quote:
    symbol: str
    ltp: float                       # last traded price
    volume: Optional[int] = None      # cumulative traded volume today
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Funds:
    """Wallet / margin snapshot, used both for the Trade Terminal's balance
    display and for the pre-trade 'do we actually have the cash for this'
    check in ExecutionEngine (see engine.py)."""
    available_cash: float             # what's actually free to trade with right now
    net: Optional[float] = None       # broker's total net (cash + collateral - utilised), if it reports one
    utilised_debits: Optional[float] = None
    collateral: Optional[float] = None
    raw: dict = field(default_factory=dict)


class BrokerError(Exception):
    """Raised for any broker-side failure (auth, rejection, network)."""


class InsufficientFundsError(BrokerError):
    """Raised when a trade (manual or algo) would need more cash than the
    wallet currently has available. Kept as its own exception (rather than
    a generic BrokerError) so callers — the agent's place_trade_order tool
    in particular — can distinguish 'not enough money' from every other
    broker failure and say so plainly instead of a generic error."""

    def __init__(self, required: float, available: float, message: str | None = None):
        self.required = required
        self.available = available
        super().__init__(
            message or (
                f"Insufficient balance: this order needs ~₹{required:,.2f} but only "
                f"₹{available:,.2f} is available in the wallet."
            )
        )


class BrokerClient(ABC):
    """Minimal interface the algo engine depends on."""

    name: str = "base"

    @abstractmethod
    async def get_quote(self, symbol: str, exchange: str) -> Quote:
        ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        ...

    @abstractmethod
    async def get_positions(self) -> list:
        ...

    @abstractmethod
    async def get_funds(self) -> Funds:
        ...