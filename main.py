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
from datetime import datetime

from config import (
    MORNING_INIT_TIME,
    DayConfig,
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
# Placeholder: replace with real broker feed or local CSV replay
# ---------------------------------------------------------------------------

async def _fetch_previous_day_ohlc() -> tuple[float, float]:
    """Return (prev_open, prev_close) for the Nifty spot index."""
    # In production: call Upstox/Fyers historical API
    # For local testing: hard-code or load from a CSV
    return 23_400.0, 23_550.0   # sample values


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
