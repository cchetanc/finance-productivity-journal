"""
Angel One (SmartAPI) broker client.

Wraps the official `smartapi-python` SDK behind the BrokerClient interface.
Auth uses the standard Angel One flow: api_key + client_code + pin + TOTP secret
(from https://smartapi.angelone.in -> create an app -> enable TOTP).

Credentials are never read from plain env vars in production; they're pulled
per-user from Firestore (encrypted) via trading.credentials.get_broker_credentials,
matching the pattern already established in app/secrets.py.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

import pyotp
from SmartApi import SmartConnect

from .broker_base import (
    BrokerClient,
    BrokerError,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)

logger = logging.getLogger("trading.angel_one")

_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "pending": OrderStatus.PENDING,
    "trigger pending": OrderStatus.PENDING,
    "complete": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


class AngelOneClient(BrokerClient):
    name = "ANGEL_ONE"

    def __init__(self, api_key: str, client_code: str, pin: str, totp_secret: str):
        self._api_key = api_key
        self._client_code = client_code
        self._pin = pin
        self._totp_secret = totp_secret
        self._conn: Optional[SmartConnect] = None
        self._feed_token: Optional[str] = None
        # A minimal in-process symbol->token cache. Angel One requires the
        # numeric instrument token (not the trading symbol) for every call,
        # sourced from their published instrument master. Populate via
        # `load_instrument(symbol, exchange, token)` at startup, or plug in
        # the instrument-master CSV lookup here.
        self._instrument_tokens: dict[str, str] = {}

    def load_instrument(self, symbol: str, exchange: str, token: str):
        self._instrument_tokens[f"{exchange}:{symbol}"] = token

    def _token_for(self, symbol: str, exchange: str) -> str:
        key = f"{exchange}:{symbol}"
        token = self._instrument_tokens.get(key)
        if not token:
            raise BrokerError(
                f"No instrument token cached for {key}. Load the Angel One "
                f"instrument master and call load_instrument() before trading it."
            )
        return token

    async def connect(self):
        """Runs the blocking SmartConnect login in a thread so it doesn't
        block the event loop."""

        def _login():
            import urllib.request
            import json
            
            conn = SmartConnect(api_key=self._api_key)
            totp = pyotp.TOTP(self._totp_secret).now()
            session = conn.generateSession(self._client_code, self._pin, totp)
            if not session.get("status"):
                raise BrokerError(f"Angel One login failed: {session.get('message')}")
            feed_token = conn.getfeedToken()
            
            # Fetch the instrument master list during connect to populate the token cache
            logger.info("Fetching Angel One instrument master...")
            req = urllib.request.Request(
                "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req) as resp:
                data = json.load(resp)
                for item in data:
                    self._instrument_tokens[f"{item['exch_seg']}:{item['symbol']}"] = item["token"]
            logger.info(f"Loaded {len(self._instrument_tokens)} instrument tokens.")
            
            return conn, feed_token

        try:
            self._conn, self._feed_token = await asyncio.to_thread(_login)
        except BrokerError:
            raise
        except Exception as e:  # noqa: BLE001 - surface SDK errors uniformly
            raise BrokerError(f"Angel One connection error: {e}") from e
        logger.info("Angel One session established for client %s", self._client_code)

    def _require_conn(self) -> SmartConnect:
        if self._conn is None:
            raise BrokerError("Not connected. Call connect() first.")
        return self._conn

    async def get_quote(self, symbol: str, exchange: str) -> Quote:
        conn = self._require_conn()
        token = self._token_for(symbol, exchange)

        def _fetch():
            return conn.ltpData(exchange, symbol, token)

        resp = await asyncio.to_thread(_fetch)
        if not resp.get("status"):
            raise BrokerError(f"ltpData failed for {symbol}: {resp.get('message')}")
        data = resp["data"]
        return Quote(symbol=symbol, ltp=float(data["ltp"]))

    async def place_order(self, order: OrderRequest) -> OrderResult:
        conn = self._require_conn()
        token = self._token_for(order.symbol, order.exchange)

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": order.symbol,
            "symboltoken": token,
            "transactiontype": order.side.value,
            "exchange": order.exchange,
            "ordertype": order.order_type.value,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(order.limit_price) if order.limit_price is not None else "0",
            "quantity": str(order.quantity),
        }
        if order.client_order_tag:
            order_params["ordertag"] = order.client_order_tag[:20]  # SDK caps tag length

        def _place():
            return conn.placeOrder(order_params)

        try:
            resp = await asyncio.to_thread(_place)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"Order placement failed: {e}") from e

        # smartapi-python's placeOrder returns the order id directly on
        # success and raises on failure, but be defensive either way.
        broker_order_id = resp if isinstance(resp, str) else resp.get("data", {}).get("orderid")
        if not broker_order_id:
            raise BrokerError(f"Order placement returned no order id: {resp}")

        return OrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.PENDING,
            placed_at=datetime.utcnow(),
            raw=order_params,
        )

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        conn = self._require_conn()

        def _cancel():
            return conn.cancelOrder(broker_order_id, "NORMAL")

        resp = await asyncio.to_thread(_cancel)
        return OrderResult(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED, raw=resp or {})

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        conn = self._require_conn()

        def _book():
            return conn.orderBook()

        resp = await asyncio.to_thread(_book)
        if not resp.get("status"):
            raise BrokerError(f"orderBook fetch failed: {resp.get('message')}")

        for row in resp.get("data") or []:
            if row.get("orderid") == broker_order_id:
                status = _STATUS_MAP.get((row.get("status") or "").lower(), OrderStatus.PENDING)
                return OrderResult(
                    broker_order_id=broker_order_id,
                    status=status,
                    filled_quantity=int(row.get("filledshares") or 0),
                    average_price=float(row["averageprice"]) if row.get("averageprice") else None,
                    raw=row,
                )
        raise BrokerError(f"Order {broker_order_id} not found in order book")

    async def get_positions(self) -> list:
        conn = self._require_conn()

        def _positions():
            return conn.position()

        resp = await asyncio.to_thread(_positions)
        if not resp.get("status"):
            raise BrokerError(f"position fetch failed: {resp.get('message')}")
        return resp.get("data") or []