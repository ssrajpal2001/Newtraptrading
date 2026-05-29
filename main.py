"""
main.py
Async entry point: wires together morning initialisation, dual-feeder
supervisor, bar aggregator, trap detectors, and expiry flusher.

Run the full engine (data + execution, no UI):
    python main.py

Run only the Streamlit UI:
    streamlit run app_ui.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote

import aiohttp

from config import (
    MORNING_INIT_TIME,
    DayConfig,
    UPSTOX_ACCESS_TOKEN,
    FYERS_APP_ID,
    FYERS_ACCESS_TOKEN,
    compute_day_config,
    is_expiry_flush_time,
    is_morning_init_window,
)
from data_feeder import (
    BarAggregator,
    DualFeederSupervisor,
    TrapDetector,
)
from database import (
    OptionType,
    flush_expired_traps,
    init_db,
)
from execution_engine import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Upstox V3 historical candle constants
# ---------------------------------------------------------------------------

_UPSTOX_HIST_URL = (
    "https://api.upstox.com/v3/historical-candle"
    "/{instrument_key}/1day/{to_date}/{from_date}"
)
# Nifty 50 spot index instrument key (URL-encoded: NSE_INDEX|Nifty 50)
_NIFTY_INSTRUMENT_KEY = "NSE_INDEX%7CNifty%2050"

# Fyers historical candle endpoint (fallback)
_FYERS_HIST_URL = "https://api.fyers.in/api/v2/history"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last_trading_day(ref: date) -> date:
    """Return the most recent weekday on or before ref (skips Sat/Sun)."""
    d = ref
    while d.weekday() >= 5:   # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Gap 1 — Real Upstox V3 historical OHLC fetch with Fyers fallback
# ---------------------------------------------------------------------------

async def _fetch_upstox_previous_ohlc() -> tuple[float, float]:
    """
    Fetch previous trading day Nifty 50 spot OPEN and CLOSE via
    Upstox V3 historical candle API.

    Endpoint:
        GET https://api.upstox.com/v3/historical-candle
             /{instrumentKey}/1day/{to_date}/{from_date}

    Response payload:
        {
          "status": "success",
          "data": {
            "candles": [
              [timestamp, open, high, low, close, volume, oi],
              ...   ← descending order, most recent first
            ]
          }
        }
    Index mapping:  0=ts  1=open  2=high  3=low  4=close  5=vol  6=oi
    """
    today     = date.today()
    prev_day  = _last_trading_day(today - timedelta(days=1))
    # Request a 5-day window to handle exchange holidays gracefully
    from_date = (prev_day - timedelta(days=5)).isoformat()
    to_date   = prev_day.isoformat()

    url = _UPSTOX_HIST_URL.format(
        instrument_key=_NIFTY_INSTRUMENT_KEY,
        to_date=to_date,
        from_date=from_date,
    )
    headers = {
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        "Accept":        "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            payload = await resp.json()

    candles = payload.get("data", {}).get("candles", [])
    if not candles:
        raise ValueError(f"No candles returned from Upstox for {from_date}→{to_date}")

    # Most recent completed day is the first element (descending order)
    latest    = candles[0]
    prev_open  = float(latest[1])   # index 1 = open
    prev_close = float(latest[4])   # index 4 = close

    logger.info(
        "Upstox historical | candle date approx %s | prev_open=%.2f prev_close=%.2f",
        to_date, prev_open, prev_close,
    )
    return prev_open, prev_close


async def _fetch_fyers_previous_ohlc() -> tuple[float, float]:
    """
    Fallback: fetch previous trading day Nifty 50 OHLC via Fyers API v2.

    Endpoint:
        GET https://api.fyers.in/api/v2/history
            ?symbol=NSE:NIFTY50-INDEX&resolution=D&date_format=1
            &range_from={epoch}&range_to={epoch}&cont_flag=1

    Response:
        { "candles": [[epoch, open, high, low, close, volume], ...] }
    Index: 0=epoch  1=open  2=high  3=low  4=close  5=vol
    """
    import time as _t

    today    = date.today()
    prev_day = _last_trading_day(today - timedelta(days=1))
    # Fyers uses Unix epoch timestamps
    from_ts  = int(datetime(prev_day.year, prev_day.month, prev_day.day, 9, 0).timestamp())
    to_ts    = int(datetime(prev_day.year, prev_day.month, prev_day.day, 16, 0).timestamp())

    params = {
        "symbol":       "NSE:NIFTY50-INDEX",
        "resolution":   "D",
        "date_format":  "1",
        "range_from":   str(from_ts),
        "range_to":     str(to_ts),
        "cont_flag":    "1",
    }
    headers = {
        "Authorization": f"{FYERS_APP_ID}:{FYERS_ACCESS_TOKEN}",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            _FYERS_HIST_URL, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()

    candles = payload.get("candles", [])
    if not candles:
        raise ValueError("No candles returned from Fyers fallback")

    latest     = candles[-1]   # Fyers returns ascending order — last = most recent
    prev_open  = float(latest[1])
    prev_close = float(latest[4])

    logger.info(
        "Fyers historical (fallback) | prev_open=%.2f prev_close=%.2f",
        prev_open, prev_close,
    )
    return prev_open, prev_close


async def _fetch_previous_day_ohlc() -> tuple[float, float]:
    """
    Fetch (prev_open, prev_close) for the Nifty 50 spot index.
    Primary: Upstox V3 historical candle API.
    Fallback: Fyers historical API.
    Last resort: raises RuntimeError so the operator is alerted.
    """
    # Primary — Upstox V3
    try:
        if UPSTOX_ACCESS_TOKEN:
            return await _fetch_upstox_previous_ohlc()
        logger.warning("UPSTOX_ACCESS_TOKEN not set — skipping primary fetch")
    except Exception as exc:
        logger.warning("Upstox historical fetch failed (%s) — trying Fyers fallback", exc)

    # Fallback — Fyers
    try:
        if FYERS_ACCESS_TOKEN:
            return await _fetch_fyers_previous_ohlc()
        logger.warning("FYERS_ACCESS_TOKEN not set — skipping Fyers fallback")
    except Exception as exc:
        logger.error("Fyers historical fetch also failed: %s", exc)

    raise RuntimeError(
        "Could not fetch previous day OHLC from either Upstox or Fyers. "
        "Check access tokens in .env and ensure market data API access is enabled."
    )


# ---------------------------------------------------------------------------
# Morning initialisation coroutine
# ---------------------------------------------------------------------------

async def morning_init() -> DayConfig:
    now      = datetime.now()
    prev_o, prev_c = await _fetch_previous_day_ohlc()
    day_cfg  = compute_day_config(now.date(), prev_o, prev_c)
    logger.info("Morning init complete: %s", day_cfg)
    return day_cfg


# ---------------------------------------------------------------------------
# Expiry flush watchdog
# ---------------------------------------------------------------------------

async def expiry_flush_watchdog(ce_symbol: str, pe_symbol: str) -> None:
    """Polls every 30 s; fires the weekly flush at Tuesday 15:30."""
    while True:
        await asyncio.sleep(30)
        if is_expiry_flush_time():
            count = flush_expired_traps([ce_symbol, pe_symbol])
            logger.info("Expiry flush: %d traps archived as EXPIRED_VOID", count)
            await asyncio.sleep(120)   # cool-off — don't double-fire


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def run() -> None:
    init_db()
    engine.reload_clients()

    # Wait for 08:45 AM window
    while not is_morning_init_window():
        logger.info("Waiting for morning init window (08:45 AM)…")
        await asyncio.sleep(10)

    day_cfg = await morning_init()

    aggregator = BarAggregator()

    # Execution callbacks wired to the engine
    def on_execute(symbol: str, price: float, trap_id: int) -> None:
        asyncio.create_task(
            engine.fire_entry(symbol, price, trap_id, quantity=1)
        )

    def on_sl_void(trap_id: int) -> None:
        asyncio.create_task(engine.fire_sl_exit(trap_id))

    # Register CE detector
    ce_detector = TrapDetector(
        symbol=day_cfg.ce_symbol,
        option_type=OptionType.CE,
        strike=day_cfg.ce_strike,
        on_execute=on_execute,
        on_sl_void=on_sl_void,
    )

    # Register PE detector
    pe_detector = TrapDetector(
        symbol=day_cfg.pe_symbol,
        option_type=OptionType.PE,
        strike=day_cfg.pe_strike,
        on_execute=on_execute,
        on_sl_void=on_sl_void,
    )

    aggregator.register_symbol(ce_detector)
    aggregator.register_symbol(pe_detector)

    feeder = DualFeederSupervisor(
        symbols=[day_cfg.ce_symbol, day_cfg.pe_symbol]
    )

    tasks = [
        asyncio.create_task(feeder.run(),       name="feeder"),
        asyncio.create_task(aggregator.run(),   name="aggregator"),
        asyncio.create_task(
            expiry_flush_watchdog(day_cfg.ce_symbol, day_cfg.pe_symbol),
            name="expiry-flush",
        ),
    ]

    logger.info("All engines running. Press Ctrl-C to stop.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown signal received — cancelling tasks.")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Signal handling for graceful shutdown
# ---------------------------------------------------------------------------

def _handle_sigint(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("SIGINT / SIGTERM received")
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_sigint, loop)

    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
        sys.exit(0)
