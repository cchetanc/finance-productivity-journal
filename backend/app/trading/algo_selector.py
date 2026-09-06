import logging
from .broker_base import BrokerClient, OrderSide, OrderType
from .algos import AlgoType, AlgoParams

logger = logging.getLogger("trading.algo_selector")

async def choose_execution_plan(broker: BrokerClient, symbol: str, exchange: str, side: OrderSide, quantity: int) -> dict:
    """
    Intelligently selects the best execution strategy based on order size, side, and current market conditions.
    Returns a dict with 'type' (either 'simple' or 'algo') and the associated parameters.
    """
    try:
        quote = await broker.get_quote(symbol, exchange)
    except Exception as e:
        logger.warning("Could not get quote for algo selection on %s: %s. Defaulting to MARKET.", symbol, e)
        return {"type": "simple", "order_type": OrderType.MARKET, "limit_price": None}

    price = quote.ltp
    if not price:
        return {"type": "simple", "order_type": OrderType.MARKET, "limit_price": None}
        
    value = price * quantity
    spread = (quote.ask - quote.bid) if (quote.ask and quote.bid) else 0

    # 1. Large/volatile -> TWAP slice strategy
    # For this system, we consider > 500,000 INR order value as "large" enough to slice
    if value > 500_000:
        logger.info("Order value %s > 500k for %s, selecting TWAP.", value, symbol)
        params = AlgoParams(
            symbol=symbol, exchange=exchange, side=side, total_quantity=quantity,
            duration_minutes=30, slice_count=5
        )
        return {"type": "algo", "algo_type": AlgoType.TWAP, "params": params}

    # 2. Sell with wide spread -> LIMIT order pegged to best bid
    # Define "wide spread" as > 0.5% of the current price
    if side == OrderSide.SELL and quote.bid and spread > (price * 0.005):
        logger.info("Wide spread %s on SELL %s, selecting LIMIT @ %s", spread, symbol, quote.bid)
        return {"type": "simple", "order_type": OrderType.LIMIT, "limit_price": quote.bid}

    # Default: MARKET order
    return {"type": "simple", "order_type": OrderType.MARKET, "limit_price": None}
