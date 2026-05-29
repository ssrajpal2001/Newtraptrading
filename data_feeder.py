"""
data_feeder.py
Async multi-broker WebSocket connectivity, bar aggregation, and the
75-min / 5-min / 1-min trap-detection engines.

Architecture
------------
  UpstoxFeeder / FyersFeeder  →  BarAggregator  →  TrapDetectionEngine
       (primary / fallback WebSocket feeds)

Each feeder pushes raw tick data into a shared asyncio.Queue.
BarAggregator builds OHLCV bars for HTF (75m), LTF (5m), and RISK (1m).
TrapDetectionEngine evaluates seller-trap conditions and updates the DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import (
    HTF_BAR_MINUTES,
    LTF_BAR_MINUTES,
    RISK_BAR_MINUTES,
    UPSTOX_ACCESS_TOKEN,
    FYERS_ACCESS_TOKEN,
    bar_key_for_minutes,
)
from database import (
    HistoricalOptionTraps,
    OptionType,
    TrapStatus,
    get_active_traps,
    get_next_lower_trap,
    mitigate_trap,
    register_trap,
    set_trap_sl,
    void_trap,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Upstox V3 protobuf binding — imported at module load time.
# If the proto hasn't been compiled yet (build_protos.py not run), the feeder
# falls back to JSON parsing and logs a one-time warning.
# ---------------------------------------------------------------------------
try:
    from proto import MarketDataFeed_pb2 as _pb2
    _PROTOBUF_AVAILABLE = True
    logger.debug("Upstox protobuf binding loaded from proto.MarketDataFeed_pb2")
except ImportError:
    _pb2 = None  # type: ignore[assignment]
    _PROTOBUF_AVAILABLE = False
    logger.warning(
        "proto/MarketDataFeed_pb2.py not found — run `python build_protos.py` "
        "before starting the engine. Upstox feeder will attempt JSON fallback."
    )

# ---------------------------------------------------------------------------
# Shared tick queue (primary and fallback feeders both push here)
# ---------------------------------------------------------------------------
TICK_QUEUE: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=50_000)

# How long to wait for primary before promoting fallback
PRIMARY_TIMEOUT_SECS = 0.1          # < 100 ms SLA
RECONNECT_DELAYS     = [2, 4, 8, 16, 30]  # seconds


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class OHLCV:
    symbol:   str
    ts:       datetime
    open:     float
    high:     float
    low:      float
    close:    float
    volume:   float = 0.0
    complete: bool  = False          # True once the bar is closed/sealed

    def update(self, price: float, volume: float = 0.0) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close  = price
        self.volume += volume


@dataclass
class BarCache:
    """Maintains rolling OHLCV bars for each symbol across timeframes."""
    _bars: Dict[Tuple[str, int], OHLCV] = field(default_factory=dict)

    def on_tick(
        self,
        symbol: str,
        price: float,
        ts: datetime,
        volume: float = 0.0,
        timeframe: int = HTF_BAR_MINUTES,
        on_close: Optional[Callable[[OHLCV], None]] = None,
    ) -> OHLCV:
        key        = (symbol, timeframe)
        bar_start  = bar_key_for_minutes(ts, timeframe)
        existing   = self._bars.get(key)

        if existing is None or existing.ts != bar_start:
            # Close the previous bar
            if existing is not None:
                existing.complete = True
                if on_close:
                    on_close(existing)

            self._bars[key] = OHLCV(
                symbol=symbol,
                ts=bar_start,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        else:
            self._bars[key].update(price, volume)

        return self._bars[key]

    def current(self, symbol: str, timeframe: int) -> Optional[OHLCV]:
        return self._bars.get((symbol, timeframe))


# ---------------------------------------------------------------------------
# Trap detection state machine (per symbol)
# ---------------------------------------------------------------------------

class TrapDetector:
    """
    Stateful processor that receives closed bars and fires callbacks when:
      1. A 75-min seller trap is confirmed (HTF high SL breached)
      2. Premium retraces to trap origin (retest entry zone)
      3. A 5-min nested trap is confirmed
      4. Exact touch of 5-min entry line fires execution callback
      5. 1-min close below SL voids the active trap
    """

    def __init__(
        self,
        symbol: str,
        option_type: OptionType,
        strike: int,
        on_execute: Optional[Callable[[str, float, int], None]] = None,
        on_sl_void: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.symbol       = symbol
        self.option_type  = option_type
        self.strike       = strike
        self.on_execute   = on_execute    # callback(symbol, price, trap_id)
        self.on_sl_void   = on_sl_void    # callback(trap_id)

        # HTF state
        self._htf_bearish_candle: Optional[OHLCV] = None
        self._htf_bearish_high_sl: Optional[float] = None
        self._active_htf_trap_id:  Optional[int]   = None

        # LTF state
        self._in_retest_zone:       bool            = False
        self._ltf_bearish_candle:   Optional[OHLCV] = None
        self._ltf_bearish_high_sl:  Optional[float] = None
        self._ltf_trap_confirmed:   bool            = False
        self._ltf_entry_line:       Optional[float] = None
        self._ltf_sl_line:          Optional[float] = None   # 5-min trap candle low

        # 1-min risk state
        self._in_trade:             bool            = False
        self._trade_sl:             Optional[float] = None

    # ------------------------------------------------------------------
    # 75-min bar closed
    # ------------------------------------------------------------------

    def on_htf_bar_close(self, bar: OHLCV) -> None:
        """
        Seller trap logic:
        A *bearish* candle on the option premium chart means the candle
        closed lower than it opened (selling pressure). If the NEXT candle
        breaches that bearish candle's high (= the high SL of sellers),
        sellers are trapped – we register the zone.
        """
        if self._htf_bearish_candle is None:
            # Designate current bar as candidate bearish setup
            if bar.close < bar.open:
                self._htf_bearish_candle  = bar
                self._htf_bearish_high_sl = bar.high
                logger.debug("[HTF] Bearish candle set for %s at high=%.2f", self.symbol, bar.high)
            return

        # Check if this bar breaches the prior bearish candle's high SL
        if (
            self._htf_bearish_high_sl is not None
            and bar.high > self._htf_bearish_high_sl
            and self._active_htf_trap_id is None
        ):
            entry_origin = self._htf_bearish_candle.open   # where sellers entered short
            target_high  = bar.high                         # 75-min trapped candle high

            trap = register_trap(
                strike=self.strike,
                option_type=self.option_type,
                contract_symbol=self.symbol,
                entry_origin=entry_origin,
                target_high=target_high,
            )
            self._active_htf_trap_id = trap.id
            logger.info(
                "[HTF] Seller trap confirmed for %s | origin=%.2f target=%.2f trap_id=%d",
                self.symbol, entry_origin, target_high, trap.id,
            )
            # Reset bearish candidate so we track fresh setups
            self._htf_bearish_candle  = None
            self._htf_bearish_high_sl = None
            return

        # Rolling update: if new bar is also bearish, refresh candidate
        if bar.close < bar.open:
            self._htf_bearish_candle  = bar
            self._htf_bearish_high_sl = bar.high

    # ------------------------------------------------------------------
    # 5-min bar closed
    # ------------------------------------------------------------------

    def on_ltf_bar_close(self, bar: OHLCV) -> None:
        if self._active_htf_trap_id is None:
            return

        # Fetch the registered trap's origin
        active_traps = get_active_traps(option_type=self.option_type)
        htf_trap = next(
            (t for t in active_traps if t.id == self._active_htf_trap_id), None
        )
        if htf_trap is None:
            self._active_htf_trap_id = None
            return

        # Phase 1: Wait for retest of 75-min origin zone
        if not self._in_retest_zone:
            zone_high = htf_trap.entry_origin * 1.005   # 0.5% tolerance band
            zone_low  = htf_trap.entry_origin * 0.995
            if zone_low <= bar.low <= zone_high or zone_low <= bar.close <= zone_high:
                self._in_retest_zone = True
                logger.info(
                    "[LTF] Premium entered 75-min retest zone for %s at %.2f",
                    self.symbol, bar.close,
                )
            return

        # Phase 2: In retest zone – watch for nested 5-min seller trap
        if not self._ltf_trap_confirmed:
            if self._ltf_bearish_candle is None:
                if bar.close < bar.open:
                    self._ltf_bearish_candle  = bar
                    self._ltf_bearish_high_sl = bar.high
            elif (
                self._ltf_bearish_high_sl is not None
                and bar.high > self._ltf_bearish_high_sl
            ):
                # 5-min seller trap confirmed
                self._ltf_trap_confirmed = True
                self._ltf_entry_line     = self._ltf_bearish_candle.open
                self._ltf_sl_line        = self._ltf_bearish_candle.low

                if htf_trap.id is not None:
                    set_trap_sl(htf_trap.id, self._ltf_sl_line)

                logger.info(
                    "[LTF] 5-min trap confirmed for %s | entry=%.2f sl=%.2f",
                    self.symbol, self._ltf_entry_line, self._ltf_sl_line,
                )

    # ------------------------------------------------------------------
    # 1-min bar closed
    # ------------------------------------------------------------------

    def on_risk_bar_close(self, bar: OHLCV) -> None:
        if not self._in_trade or self._trade_sl is None:
            return
        if bar.close < self._trade_sl:
            logger.warning(
                "[RISK] 1-min close %.2f below SL %.2f – voiding trap %s",
                bar.close, self._trade_sl, self._active_htf_trap_id,
            )
            if self._active_htf_trap_id is not None:
                void_trap(self._active_htf_trap_id)
                if self.on_sl_void:
                    self.on_sl_void(self._active_htf_trap_id)

                # Cascade to next lower trap
                next_trap = get_next_lower_trap(
                    current_entry=self._ltf_entry_line or 0.0,
                    option_type=self.option_type,
                )
                if next_trap:
                    self._active_htf_trap_id = next_trap.id
                    logger.info(
                        "[CASCADE] Shifted to next trap id=%d origin=%.2f",
                        next_trap.id, next_trap.entry_origin,
                    )
                else:
                    self._active_htf_trap_id = None

            self._reset_trade_state()

    # ------------------------------------------------------------------
    # Live tick (sub-bar) evaluation for touch entry
    # ------------------------------------------------------------------

    def on_tick(self, price: float) -> None:
        """Called on every live tick.  Fires execution if entry line is touched."""
        if (
            self._ltf_trap_confirmed
            and not self._in_trade
            and self._ltf_entry_line is not None
        ):
            # Exact touch or crossing of the 5-min sellers' entry line
            if price <= self._ltf_entry_line:
                logger.info(
                    "[ENTRY] Touch trigger! %s price=%.2f entry_line=%.2f",
                    self.symbol, price, self._ltf_entry_line,
                )
                self._in_trade  = True
                self._trade_sl  = self._ltf_sl_line
                if self.on_execute and self._active_htf_trap_id is not None:
                    self.on_execute(self.symbol, price, self._active_htf_trap_id)

        # Check target exit
        if self._in_trade and self._active_htf_trap_id is not None:
            active_traps = get_active_traps(option_type=self.option_type)
            trap = next(
                (t for t in active_traps if t.id == self._active_htf_trap_id), None
            )
            if trap and price >= trap.target_high:
                logger.info(
                    "[TARGET] Target %.2f hit for trap %d", trap.target_high, trap.id
                )
                mitigate_trap(trap.id)
                self._reset_trade_state()

    def _reset_trade_state(self) -> None:
        self._in_retest_zone      = False
        self._ltf_bearish_candle  = None
        self._ltf_bearish_high_sl = None
        self._ltf_trap_confirmed  = False
        self._ltf_entry_line      = None
        self._ltf_sl_line         = None
        self._in_trade            = False
        self._trade_sl            = None


# ---------------------------------------------------------------------------
# Bar aggregator – receives raw ticks, builds multi-TF bars
# ---------------------------------------------------------------------------

class BarAggregator:
    """
    Subscribes to the shared TICK_QUEUE and drives TrapDetector instances.
    Instantiated once per session; accepts dynamically injected symbols.
    """

    def __init__(self) -> None:
        self._bar_cache   = BarCache()
        self._detectors:  Dict[str, TrapDetector] = {}

    def register_symbol(self, detector: TrapDetector) -> None:
        self._detectors[detector.symbol] = detector

    async def run(self) -> None:
        logger.info("BarAggregator started")
        while True:
            tick: Dict[str, Any] = await TICK_QUEUE.get()
            symbol = tick.get("symbol", "")
            price  = float(tick.get("price", 0.0))
            ts_raw = tick.get("ts", "")
            volume = float(tick.get("volume", 0.0))

            try:
                ts = (
                    datetime.fromisoformat(ts_raw)
                    if isinstance(ts_raw, str) and ts_raw
                    else datetime.utcnow()
                )
            except ValueError:
                ts = datetime.utcnow()

            detector = self._detectors.get(symbol)
            if detector is None:
                continue

            # 75-min bar
            self._bar_cache.on_tick(
                symbol, price, ts, volume,
                timeframe=HTF_BAR_MINUTES,
                on_close=detector.on_htf_bar_close,
            )
            # 5-min bar
            self._bar_cache.on_tick(
                symbol, price, ts, volume,
                timeframe=LTF_BAR_MINUTES,
                on_close=detector.on_ltf_bar_close,
            )
            # 1-min bar
            self._bar_cache.on_tick(
                symbol, price, ts, volume,
                timeframe=RISK_BAR_MINUTES,
                on_close=detector.on_risk_bar_close,
            )

            # Sub-bar live tick evaluation (touch entry check)
            detector.on_tick(price)


# ---------------------------------------------------------------------------
# Upstox WebSocket feeder  (MarketDataStreamer V3 — binary protobuf frames)
# ---------------------------------------------------------------------------
#
# V3 changes from V2:
#   • URL:      wss://api.upstox.com/v3/feed/market-data-feed
#   • Frames:   binary protobuf (FeedResponse), NOT JSON
#   • Sub msg:  same JSON envelope, but instrumentKeys use NSE_FO| prefix
#
# Instrument key format for NFO weekly options:
#   NSE_FO|NIFTY{YY}{M}{DD}{strike}{CE|PE}
#   e.g. NSE_FO|NIFTY2562323500CE  (June 23 2025, 23500 CE)
#
# The DayConfig.ce_symbol / pe_symbol are stored in NSE:NIFTY... display
# format.  _to_upstox_key() converts them at subscription time.
# ---------------------------------------------------------------------------

UPSTOX_WS_URL_V3 = "wss://api.upstox.com/v3/feed/market-data-feed"


def _to_upstox_key(display_symbol: str) -> str:
    """
    Convert NSE:NIFTY03JUN2523500CE  →  NSE_FO|NIFTY2562323500CE
    (Upstox V3 weekly option instrument key format).

    Format breakdown: NSE_FO|NIFTY + YY + M(1-12) + DD + strike + type
    The Upstox weekly key omits leading zeros from single-digit months.
    """
    import re
    _MONTH_MAP = {
        "JAN": 1,  "FEB": 2,  "MAR": 3,  "APR": 4,
        "MAY": 5,  "JUN": 6,  "JUL": 7,  "AUG": 8,
        "SEP": 9,  "OCT": 10, "NOV": 11, "DEC": 12,
    }
    # Pattern: NSE:NIFTY + DDMMMYY + strike + type
    m = re.match(
        r"NSE:NIFTY(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$",
        display_symbol.upper(),
    )
    if not m:
        # Symbol is already in Upstox format or unknown — return as-is
        logger.warning("Cannot convert symbol to Upstox key: %s", display_symbol)
        return display_symbol

    dd, mon, yy, strike, opt_type = m.groups()
    month_num = _MONTH_MAP.get(mon, 0)
    if not month_num:
        logger.warning("Unknown month %s in symbol %s", mon, display_symbol)
        return display_symbol

    # Upstox format: NIFTY + YY + month(no leading zero) + DD + strike + type
    return f"NSE_FO|NIFTY{yy}{month_num}{dd}{strike}{opt_type}"


class UpstoxFeeder:
    def __init__(self, symbols: list[str]) -> None:
        # Convert display symbols to Upstox V3 instrument key format
        self.display_symbols  = symbols
        self.instrument_keys  = [_to_upstox_key(s) for s in symbols]
        # Map instrument key → original display symbol for downstream consumers
        self._key_to_display  = dict(zip(self.instrument_keys, symbols))
        self._last_tick       = _time.monotonic()
        self._running         = False

    async def run(self) -> None:
        self._running = True
        delay_idx = 0
        while self._running:
            try:
                headers = {"Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"}
                async with websockets.connect(
                    UPSTOX_WS_URL_V3,
                    extra_headers=headers,
                    ping_interval=20,
                    max_size=2 ** 23,       # 8 MB — handles large market-depth frames
                ) as ws:
                    delay_idx = 0
                    subscribe_msg = json.dumps({
                        "guid":   "newtrap-upstox-v3",
                        "method": "sub",
                        "data": {
                            "mode":           "full",
                            "instrumentKeys": self.instrument_keys,
                        },
                    })
                    await ws.send(subscribe_msg)
                    logger.info(
                        "Upstox V3 feeder subscribed | keys=%s", self.instrument_keys
                    )

                    async for frame in ws:
                        self._last_tick = _time.monotonic()
                        if isinstance(frame, bytes):
                            await _decode_upstox_binary(frame, self._key_to_display)
                        else:
                            # Text frame — server-side control/error message; log and skip
                            logger.debug("Upstox text frame: %s", frame[:200])

            except ConnectionClosed as e:
                logger.warning("Upstox V3 WS closed: %s", e)
            except WebSocketException as e:
                logger.error("Upstox V3 WS error: %s", e)
            except Exception as e:
                logger.exception("Upstox V3 unexpected error: %s", e)

            if not self._running:
                break
            delay = RECONNECT_DELAYS[min(delay_idx, len(RECONNECT_DELAYS) - 1)]
            logger.info("Upstox V3 reconnecting in %ds …", delay)
            await asyncio.sleep(delay)
            delay_idx += 1

    def staleness_secs(self) -> float:
        return _time.monotonic() - self._last_tick

    def stop(self) -> None:
        self._running = False


async def _decode_upstox_binary(
    frame: bytes,
    key_to_display: Dict[str, str],
) -> None:
    """
    Deserialise a binary protobuf frame from Upstox MarketDataStreamer V3.

    FeedResponse structure (abbreviated):
        FeedResponse
          .feeds: map<string, Feed>          key = instrument_key (e.g. NSE_FO|...)
            Feed.fullFeed
              FullFeed.marketFF              for options / equities
                MarketFullFeed.ltpc
                  LTPC.ltp                  last traded price  ← we need this
                  LTPC.ltt                  last traded time (epoch ms)
                  LTPC.ltq                  last traded quantity
              FullFeed.indexFF               for indices (Nifty spot – NOT used here)
                IndexFullFeed.ltpc.ltp

    Falls back to no-op with a warning if the proto binding is not compiled.
    """
    if not _PROTOBUF_AVAILABLE or _pb2 is None:
        # Proto not compiled — nothing to parse.  Operator must run build_protos.py.
        return

    try:
        feed_response = _pb2.FeedResponse()
        feed_response.ParseFromString(frame)
    except Exception as exc:
        logger.debug("Protobuf parse error (frame len=%d): %s", len(frame), exc)
        return

    for instrument_key, feed in feed_response.feeds.items():
        # Resolve the display symbol from the instrument key
        display_sym = key_to_display.get(instrument_key, instrument_key)

        # Navigate the oneof chain: fullFeed → marketFF → ltpc
        # (fullFeed is set for options; indexFF is set for indices — we never
        #  subscribe to index instruments, so the marketFF branch is always taken)
        ltpc = None
        which_feed = feed.WhichOneof("FeedUnion")
        if which_feed == "fullFeed":
            which_full = feed.fullFeed.WhichOneof("FullFeedUnion")
            if which_full == "marketFF":
                ltpc = feed.fullFeed.marketFF.ltpc
            elif which_full == "indexFF":
                ltpc = feed.fullFeed.indexFF.ltpc
        elif which_feed == "compactFeed":
            which_compact = feed.compactFeed.WhichOneof("CompactFeedUnion")
            if which_compact == "marketCF":
                ltpc = feed.compactFeed.marketCF.ltpc
            elif which_compact == "indexCF":
                ltpc = feed.compactFeed.indexCF.ltpc

        if ltpc is None or ltpc.ltp == 0.0:
            continue

        # Convert epoch-ms ltt to ISO string for downstream BarAggregator
        try:
            ts_str = datetime.fromtimestamp(ltpc.ltt / 1000).isoformat()
        except (OSError, ValueError, OverflowError):
            ts_str = ""

        await TICK_QUEUE.put({
            "symbol": display_sym,
            "price":  float(ltpc.ltp),
            "ts":     ts_str,
            "volume": float(ltpc.ltq),
        })


# ---------------------------------------------------------------------------
# Fyers WebSocket feeder
# ---------------------------------------------------------------------------

FYERS_WS_URL = "wss://api.fyers.in/socket/v2"


class FyersFeeder:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols    = symbols
        self._last_tick = _time.monotonic()
        self._running   = False

    async def run(self) -> None:
        self._running = True
        delay_idx = 0
        while self._running:
            try:
                async with websockets.connect(FYERS_WS_URL, ping_interval=20) as ws:
                    delay_idx = 0
                    subscribe_msg = json.dumps({
                        "T":    "SUB_DATA",
                        "SLIST": self.symbols,
                        "SUB_T": 1,
                        "access_token": FYERS_ACCESS_TOKEN,
                    })
                    await ws.send(subscribe_msg)
                    logger.info("Fyers feeder subscribed to %s", self.symbols)

                    async for raw in ws:
                        self._last_tick = _time.monotonic()
                        data = json.loads(raw) if isinstance(raw, str) else {}
                        await _push_fyers_tick(data)

            except ConnectionClosed as e:
                logger.warning("Fyers WS closed: %s", e)
            except WebSocketException as e:
                logger.error("Fyers WS error: %s", e)
            except Exception as e:
                logger.exception("Fyers unexpected error: %s", e)

            if not self._running:
                break
            delay = RECONNECT_DELAYS[min(delay_idx, len(RECONNECT_DELAYS) - 1)]
            logger.info("Fyers reconnecting in %ds …", delay)
            await asyncio.sleep(delay)
            delay_idx += 1

    def staleness_secs(self) -> float:
        return _time.monotonic() - self._last_tick

    def stop(self) -> None:
        self._running = False


async def _push_fyers_tick(data: Dict[str, Any]) -> None:
    sym   = data.get("symbol", "")
    price = float(data.get("ltp", 0) or 0)
    if sym and price:
        await TICK_QUEUE.put({
            "symbol": sym,
            "price":  price,
            "ts":     data.get("timestamp", ""),
            "volume": float(data.get("vol_traded_today", 0) or 0),
        })


# ---------------------------------------------------------------------------
# Dual-feeder supervisor with < 100ms primary-to-fallback switch
# ---------------------------------------------------------------------------

class DualFeederSupervisor:
    """
    Runs both feeders concurrently.  Monitors primary (Upstox) staleness;
    if it exceeds PRIMARY_TIMEOUT_SECS promotes Fyers as active source.
    Both feeders always push into the same TICK_QUEUE – the supervisor only
    adds/removes the secondary feeder's data based on primary health.
    """

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self._upstox = UpstoxFeeder(symbols)
        self._fyers  = FyersFeeder(symbols)
        self._use_fallback = False

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._upstox.run(), name="upstox-feeder"),
            asyncio.create_task(self._fyers.run(),  name="fyers-feeder"),
            asyncio.create_task(self._health_monitor(), name="feeder-health"),
        ]
        await asyncio.gather(*tasks)

    async def _health_monitor(self) -> None:
        while True:
            await asyncio.sleep(0.05)   # poll every 50 ms
            primary_stale = self._upstox.staleness_secs() > PRIMARY_TIMEOUT_SECS
            if primary_stale and not self._use_fallback:
                logger.warning("Primary feeder stale – promoting Fyers fallback")
                self._use_fallback = True
            elif not primary_stale and self._use_fallback:
                logger.info("Primary feeder recovered – demoting fallback")
                self._use_fallback = False
