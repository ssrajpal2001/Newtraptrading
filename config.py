"""
config.py
Morning anchor calculations, strike selection, ITM distance matrix,
and session-level configuration constants.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Broker / API credentials – populate via environment variables or a secrets
# manager; never hard-code credentials in source.
# ---------------------------------------------------------------------------
import os

UPSTOX_API_KEY    = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

FYERS_APP_ID      = os.getenv("FYERS_APP_ID", "")
FYERS_SECRET_KEY  = os.getenv("FYERS_SECRET_KEY", "")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")

# ---------------------------------------------------------------------------
# Market session constants
# ---------------------------------------------------------------------------
MARKET_OPEN          = time(9, 15)
MARKET_CLOSE         = time(15, 30)
MORNING_INIT_TIME    = time(8, 45)
EXPIRY_FLUSH_TIME    = time(15, 30)      # Tuesday flush trigger
EXPIRY_WEEKDAY       = 1                 # Tuesday = 1 (Mon=0)

NIFTY_STRIKE_STEP    = 50               # Nearest rounding interval (points)
HTF_BAR_MINUTES      = 75
LTF_BAR_MINUTES      = 5
RISK_BAR_MINUTES     = 1

# ---------------------------------------------------------------------------
# ITM Distance Matrix  (weekday → offset in Nifty points)
# Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4
# ---------------------------------------------------------------------------
ITM_DISTANCE_MATRIX: dict[int, int] = {
    0: 200,   # Monday
    1: 100,   # Tuesday  (Expiry Day)
    2: 500,   # Wednesday
    3: 400,   # Thursday
    4: 300,   # Friday
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///newtrap_trading.db")

# ---------------------------------------------------------------------------
# Streamlit / UI
# ---------------------------------------------------------------------------
UI_REFRESH_INTERVAL_SECS = 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DayConfig:
    """Holds the computed session parameters produced at 08:45 AM."""
    date:          date
    center_point:  float
    itm_offset:    int
    ce_strike:     int
    pe_strike:     int
    ce_symbol:     str = field(default="")
    pe_symbol:     str = field(default="")

    def __post_init__(self) -> None:
        # Derive broker symbol strings once strikes are known
        expiry_str = _next_tuesday_expiry_str(self.date)
        self.ce_symbol = f"NSE:NIFTY{expiry_str}{self.ce_strike}CE"
        self.pe_symbol = f"NSE:NIFTY{expiry_str}{self.pe_strike}PE"


# ---------------------------------------------------------------------------
# Core mathematical helpers
# ---------------------------------------------------------------------------

def compute_center_point(prev_open: float, prev_close: float) -> float:
    """Day Center Point = (Previous Day Open + Previous Day Close) / 2."""
    return (prev_open + prev_close) / 2.0


def round_to_strike(value: float, step: int = NIFTY_STRIKE_STEP) -> int:
    """Round a float to the nearest Nifty strike interval."""
    return int(round(value / step) * step)


def compute_day_config(
    trade_date: date,
    prev_open: float,
    prev_close: float,
) -> DayConfig:
    """
    Full morning anchor computation:
      1. Center Point from previous session OHLC
      2. ITM offset lookup by weekday
      3. CE / PE strike rounding
    """
    center = compute_center_point(prev_open, prev_close)
    weekday = trade_date.weekday()
    offset  = ITM_DISTANCE_MATRIX.get(weekday, 200)

    ce_raw = center - offset          # CE tracked: below center
    pe_raw = center + offset          # PE tracked: above center

    ce_strike = round_to_strike(ce_raw)
    pe_strike = round_to_strike(pe_raw)

    logger.info(
        "DayConfig | date=%s center=%.2f offset=%d CE=%d PE=%d",
        trade_date, center, offset, ce_strike, pe_strike,
    )
    return DayConfig(
        date=trade_date,
        center_point=center,
        itm_offset=offset,
        ce_strike=ce_strike,
        pe_strike=pe_strike,
    )


def _next_tuesday_expiry_str(ref_date: date) -> str:
    """Return nearest upcoming Tuesday in DDMMMYY format (Nifty symbol convention)."""
    from datetime import timedelta
    days_ahead = (1 - ref_date.weekday()) % 7   # 1 = Tuesday
    if days_ahead == 0:
        days_ahead = 0
    expiry = ref_date + timedelta(days=days_ahead)
    return expiry.strftime("%d%b%y").upper()


def is_market_open(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE and now.weekday() < 5


def is_morning_init_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    t = now.time()
    return t >= MORNING_INIT_TIME and now.weekday() < 5


def is_expiry_flush_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.weekday() == EXPIRY_WEEKDAY and now.time() >= EXPIRY_FLUSH_TIME


# ---------------------------------------------------------------------------
# Bar-aggregation helpers shared across modules
# ---------------------------------------------------------------------------

def bar_key_for_minutes(ts: datetime, minutes: int) -> datetime:
    """Truncate a timestamp to the start of its N-minute bar."""
    total_minutes = ts.hour * 60 + ts.minute
    bar_start_minutes = (total_minutes // minutes) * minutes
    return ts.replace(
        hour=bar_start_minutes // 60,
        minute=bar_start_minutes % 60,
        second=0,
        microsecond=0,
    )
