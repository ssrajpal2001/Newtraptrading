"""
execution_engine.py
Asynchronous parallel order distribution across multiple client broker accounts.

Supported brokers (modular adapters):
  • Zerodha Kite Connect
  • Angel One SmartAPI
  • Alice Blue ANT API
  • Groww API

On a touch-entry trigger the ExecutionEngine fans out market BUY orders
concurrently across every active client via asyncio.gather, records each
trade in the ledger, and exposes a global panic square-off coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import LOT_SIZE
from database import (
    ClientsRegistry,
    ExitCategory,
    get_all_active_clients,
    record_trade_entry,
    record_trade_exit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory open positions registry: client_id → (trade_id, symbol, entry_px, quantity)
# ---------------------------------------------------------------------------
_OPEN_POSITIONS: Dict[int, Tuple[int, str, float, int]] = {}


# ---------------------------------------------------------------------------
# Broker adapter base
# ---------------------------------------------------------------------------

class BrokerAdapter(ABC):
    """Abstract base for all broker order-routing adapters."""

    def __init__(self, client: ClientsRegistry) -> None:
        self.client = client

    @abstractmethod
    async def place_market_buy(self, symbol: str, quantity: int) -> Dict[str, Any]:
        """Place a market buy order; return broker response dict."""

    @abstractmethod
    async def place_market_sell(self, symbol: str, quantity: int) -> Dict[str, Any]:
        """Place a market sell order (square-off); return broker response dict."""

    @abstractmethod
    async def get_ltp(self, symbol: str) -> float:
        """Fetch the last traded price for square-off pricing."""

    @abstractmethod
    async def get_margin(self) -> float:
        """Return the available margin / cash balance."""


# ---------------------------------------------------------------------------
# Zerodha Kite adapter
# ---------------------------------------------------------------------------

class ZerodhaAdapter(BrokerAdapter):

    async def _session(self):
        try:
            from kiteconnect import KiteConnect  # type: ignore
            kite = KiteConnect(api_key=self.client.api_key)
            kite.set_access_token(self.client.access_token)
            return kite
        except ImportError:
            raise RuntimeError("kiteconnect package not installed")

    async def place_market_buy(self, symbol: str, quantity: int) -> Dict[str, Any]:
        from kiteconnect import KiteConnect  # type: ignore
        kite = await self._session()
        order_id = kite.place_order(
            tradingsymbol=symbol,
            exchange="NFO",
            transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
            quantity=quantity,
            order_type=KiteConnect.ORDER_TYPE_MARKET,
            product=KiteConnect.PRODUCT_MIS,
            variety=KiteConnect.VARIETY_REGULAR,
        )
        return {"order_id": order_id, "status": "PLACED"}

    async def place_market_sell(self, symbol: str, quantity: int) -> Dict[str, Any]:
        from kiteconnect import KiteConnect  # type: ignore
        kite = await self._session()
        order_id = kite.place_order(
            tradingsymbol=symbol,
            exchange="NFO",
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            order_type=KiteConnect.ORDER_TYPE_MARKET,
            product=KiteConnect.PRODUCT_MIS,
            variety=KiteConnect.VARIETY_REGULAR,
        )
        return {"order_id": order_id, "status": "PLACED"}

    async def get_ltp(self, symbol: str) -> float:
        from kiteconnect import KiteConnect  # type: ignore
        kite = await self._session()
        data = kite.ltp(f"NFO:{symbol}")
        return float(data.get(f"NFO:{symbol}", {}).get("last_price", 0))

    async def get_margin(self) -> float:
        from kiteconnect import KiteConnect  # type: ignore
        kite = await self._session()
        margins = kite.margins()
        return float(margins.get("equity", {}).get("available", {}).get("live_balance", 0))


# ---------------------------------------------------------------------------
# Angel One adapter
# ---------------------------------------------------------------------------

class AngelOneAdapter(BrokerAdapter):

    async def _session(self):
        try:
            from SmartApi import SmartConnect  # type: ignore
            import pyotp
            obj = SmartConnect(api_key=self.client.api_key)
            totp = pyotp.TOTP(self.client.totp_secret).now()
            obj.generateSession(self.client.api_key, self.client.access_token, totp)
            return obj
        except ImportError:
            raise RuntimeError("SmartApi / pyotp package not installed")

    async def place_market_buy(self, symbol: str, quantity: int) -> Dict[str, Any]:
        obj = await self._session()
        resp = obj.placeOrder({
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": "",
            "transactiontype": "BUY",
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": quantity,
        })
        return resp

    async def place_market_sell(self, symbol: str, quantity: int) -> Dict[str, Any]:
        obj = await self._session()
        resp = obj.placeOrder({
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": "",
            "transactiontype": "SELL",
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": quantity,
        })
        return resp

    async def get_ltp(self, symbol: str) -> float:
        obj = await self._session()
        data = obj.ltpData("NFO", symbol, "")
        return float(data.get("data", {}).get("ltp", 0))

    async def get_margin(self) -> float:
        obj = await self._session()
        data = obj.rmsLimit()
        return float(data.get("data", {}).get("availablecash", 0))


# ---------------------------------------------------------------------------
# Alice Blue adapter
# ---------------------------------------------------------------------------

class AliceBlueAdapter(BrokerAdapter):

    async def _session(self):
        try:
            from alice_blue import AliceBlue  # type: ignore
            return AliceBlue(
                username=self.client.api_key,
                password=self.client.api_secret,
                access_token=self.client.access_token,
            )
        except ImportError:
            raise RuntimeError("alice_blue package not installed")

    async def place_market_buy(self, symbol: str, quantity: int) -> Dict[str, Any]:
        alice = await self._session()
        instrument = alice.get_instrument_for_fno("NFO", symbol)
        order_id = alice.place_order(
            transaction_type=alice.TransactionType.Buy,
            instrument=instrument,
            quantity=quantity,
            order_type=alice.OrderType.Market,
            product_type=alice.ProductType.Intraday,
        )
        return {"order_id": order_id}

    async def place_market_sell(self, symbol: str, quantity: int) -> Dict[str, Any]:
        alice = await self._session()
        instrument = alice.get_instrument_for_fno("NFO", symbol)
        order_id = alice.place_order(
            transaction_type=alice.TransactionType.Sell,
            instrument=instrument,
            quantity=quantity,
            order_type=alice.OrderType.Market,
            product_type=alice.ProductType.Intraday,
        )
        return {"order_id": order_id}

    async def get_ltp(self, symbol: str) -> float:
        alice = await self._session()
        instrument = alice.get_instrument_for_fno("NFO", symbol)
        data = alice.get_market_depth(instrument)
        return float(data.get("ltp", 0))

    async def get_margin(self) -> float:
        alice = await self._session()
        data = alice.get_balance()
        return float(data.get("cashPositions", {}).get("payInAmt", 0))


# ---------------------------------------------------------------------------
# Groww adapter
# ---------------------------------------------------------------------------

class GrowwAdapter(BrokerAdapter):
    """
    Groww's trading API is accessed via their published REST endpoints.
    Credentials: api_key = client_id, access_token = auth bearer token.
    """

    _BASE = "https://api.groww.in/v1"

    async def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.client.access_token}",
            "Content-Type":  "application/json",
        }

    async def _post(self, path: str, payload: Dict) -> Dict:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._BASE}{path}",
                json=payload,
                headers=await self._headers(),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._BASE}{path}",
                params=params,
                headers=await self._headers(),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def place_market_buy(self, symbol: str, quantity: int) -> Dict[str, Any]:
        return await self._post("/orders/place", {
            "trading_symbol": symbol,
            "exchange":        "NFO",
            "transaction_type": "BUY",
            "order_type":      "MARKET",
            "quantity":        quantity,
            "product":         "INTRADAY",
        })

    async def place_market_sell(self, symbol: str, quantity: int) -> Dict[str, Any]:
        return await self._post("/orders/place", {
            "trading_symbol": symbol,
            "exchange":        "NFO",
            "transaction_type": "SELL",
            "order_type":      "MARKET",
            "quantity":        quantity,
            "product":         "INTRADAY",
        })

    async def get_ltp(self, symbol: str) -> float:
        data = await self._get("/market/quote", {"symbol": symbol, "exchange": "NFO"})
        return float(data.get("ltp", 0))

    async def get_margin(self) -> float:
        data = await self._get("/user/margin")
        return float(data.get("available_margin", 0))


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------

_ADAPTER_MAP = {
    "ZERODHA":    ZerodhaAdapter,
    "ANGEL_ONE":  AngelOneAdapter,
    "ALICE_BLUE": AliceBlueAdapter,
    "GROWW":      GrowwAdapter,
}


def build_adapter(client: ClientsRegistry) -> BrokerAdapter:
    broker_key = client.broker.value if hasattr(client.broker, "value") else str(client.broker)
    cls = _ADAPTER_MAP.get(broker_key)
    if cls is None:
        raise ValueError(f"No adapter for broker: {broker_key}")
    return cls(client)


# ---------------------------------------------------------------------------
# Execution Engine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """
    Dispatches simultaneous market orders to all active clients when a
    5-min touch entry trigger fires.  All I/O is async; orders fly in
    parallel via asyncio.gather.
    """

    def __init__(self) -> None:
        self._clients: List[ClientsRegistry] = []
        self._adapters: Dict[int, BrokerAdapter] = {}

    def reload_clients(self) -> None:
        self._clients  = get_all_active_clients()
        self._adapters = {c.id: build_adapter(c) for c in self._clients}
        logger.info("ExecutionEngine loaded %d active clients", len(self._clients))

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def fire_entry(
        self,
        symbol: str,
        entry_price: float,
        trap_id: int,
        quantity: int = 0,
    ) -> None:
        """
        Fan-out market BUY to all clients concurrently.

        quantity=0 (default) means auto-compute per client from max_capital:
            num_lots = floor(max_capital / (entry_price * LOT_SIZE))
            quantity = max(1, num_lots) * LOT_SIZE
        Pass an explicit positive quantity to override for all clients.
        """
        if not self._clients:
            self.reload_clients()

        logger.info(
            "FIRE ENTRY | symbol=%s entry=%.2f trap=%d clients=%d",
            symbol, entry_price, trap_id, len(self._clients),
        )
        tasks = [
            self._place_buy_for_client(c, symbol, entry_price, quantity, trap_id)
            for c in self._clients
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for client, result in zip(self._clients, results):
            if isinstance(result, Exception):
                logger.error("Order failed for client %s: %s", client.name, result)

    @staticmethod
    def _compute_quantity(client: ClientsRegistry, entry_price: float, override: int) -> int:
        """
        Compute lot-aligned order quantity for a client.
        override > 0: use that value as-is.
        override == 0: derive from max_capital.
          num_lots = floor(max_capital / (entry_price * LOT_SIZE)), minimum 1 lot.
          quantity = num_lots * LOT_SIZE
        """
        if override > 0:
            return override
        if entry_price <= 0:
            return LOT_SIZE   # fallback: 1 lot
        num_lots = max(1, math.floor((client.max_capital or LOT_SIZE) / (entry_price * LOT_SIZE)))
        return num_lots * LOT_SIZE

    async def _place_buy_for_client(
        self,
        client: ClientsRegistry,
        symbol: str,
        entry_price: float,
        quantity_override: int,
        trap_id: int,
    ) -> None:
        quantity = self._compute_quantity(client, entry_price, quantity_override)
        adapter  = self._adapters[client.id]
        try:
            resp = await adapter.place_market_buy(symbol, quantity)
            trade = record_trade_entry(
                client_id=client.id,
                contract_symbol=symbol,
                entry_price=entry_price,
                quantity=quantity,
                trap_id=trap_id,
            )
            _OPEN_POSITIONS[client.id] = (trade.id, symbol, entry_price, quantity)
            logger.info(
                "BUY placed | client=%s qty=%d (max_cap=%.0f) trade_id=%d resp=%s",
                client.name, quantity, client.max_capital or 0, trade.id, resp,
            )
        except Exception as exc:
            logger.exception("BUY error for client %s: %s", client.name, exc)
            raise

    # ------------------------------------------------------------------
    # SL exit
    # ------------------------------------------------------------------

    async def fire_sl_exit(self, trap_id: int) -> None:
        """Square off all positions tied to this trap when SL is hit."""
        await self._close_all_positions(ExitCategory.SL_HIT)

    # ------------------------------------------------------------------
    # Target exit
    # ------------------------------------------------------------------

    async def fire_target_exit(self, trap_id: int) -> None:
        await self._close_all_positions(ExitCategory.TARGET_HIT)

    # ------------------------------------------------------------------
    # Panic / global square-off (UI master button)
    # ------------------------------------------------------------------

    async def panic_square_off_all(self) -> None:
        """Instantly square off every open position across all clients."""
        logger.critical("PANIC SQUARE-OFF INITIATED")
        await self._close_all_positions(ExitCategory.MANUAL_SQUARE)

    async def _close_all_positions(self, exit_category: ExitCategory) -> None:
        if not self._clients:
            self.reload_clients()

        tasks = []
        for client in self._clients:
            pos = _OPEN_POSITIONS.get(client.id)
            if pos is None:
                continue
            trade_id, symbol, _entry_px, quantity = pos
            tasks.append(
                self._close_position_for_client(client, trade_id, symbol, quantity, exit_category)
            )

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for client, result in zip(
                [c for c in self._clients if c.id in _OPEN_POSITIONS], results
            ):
                if isinstance(result, Exception):
                    logger.error("Exit failed for client %s: %s", client.name, result)

    async def _close_position_for_client(
        self,
        client: ClientsRegistry,
        trade_id: int,
        symbol: str,
        quantity: int,
        exit_category: ExitCategory,
    ) -> None:
        adapter = self._adapters[client.id]
        try:
            exit_price = await adapter.get_ltp(symbol)
            await adapter.place_market_sell(symbol, quantity=quantity)
            record_trade_exit(trade_id, exit_price, exit_category)
            _OPEN_POSITIONS.pop(client.id, None)
            logger.info(
                "EXIT placed | client=%s qty=%d trade_id=%d exit=%.2f cat=%s",
                client.name, quantity, trade_id, exit_price, exit_category,
            )
        except Exception as exc:
            logger.exception("EXIT error for client %s: %s", client.name, exc)
            raise

    # ------------------------------------------------------------------
    # Margin snapshot (for UI dashboard)
    # ------------------------------------------------------------------

    async def get_all_margins(self) -> Dict[str, float]:
        if not self._clients:
            self.reload_clients()
        tasks  = {c.name: self._adapters[c.id].get_margin() for c in self._clients}
        result = {}
        for name, coro in tasks.items():
            try:
                result[name] = await coro
            except Exception as exc:
                logger.warning("Margin fetch failed for %s: %s", name, exc)
                result[name] = 0.0
        return result

    def get_open_positions(self) -> Dict[int, Tuple[int, str, float, int]]:
        return dict(_OPEN_POSITIONS)


# Module-level singleton
engine = ExecutionEngine()
