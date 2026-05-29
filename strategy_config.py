"""
strategy_config.py
Thread-safe, mutable strategy parameter singleton.

All operational thresholds (timeframes, zone sizes, slippage) are stored
here rather than as hardcoded constants.  The Streamlit Admin Panel writes
to this object; the engine reads from it.

Timeframe changes (HTF / MTF / LTF) take effect on the next engine restart
because in-progress bar boundaries cannot be safely shifted mid-session.
All other parameters (retest zone %, slippage %) are applied instantly on
the next bar or tick that reads them.
"""

from __future__ import annotations

import threading
from typing import Optional

# Defaults sourced from config constants (avoids circular imports)
from config import HTF_BAR_MINUTES, LTF_BAR_MINUTES, RISK_BAR_MINUTES


class StrategyConfig:
    """
    Mutable strategy parameters shared between the engine and the UI.
    All attribute access is protected by a single re-entrant lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # --- Timeframe settings (require engine restart to take full effect) ---
        self._htf_minutes:  int   = HTF_BAR_MINUTES    # default 75
        self._mtf_minutes:  int   = LTF_BAR_MINUTES    # default 5
        self._ltf_minutes:  int   = RISK_BAR_MINUTES   # default 1

        # --- Live-adjustable thresholds ---
        self._retest_zone_pct: float = 0.5    # ±0.5% band around 75-min origin
        self._slippage_pct:    float = 0.0    # extra buffer on market orders (0 = none)

    # ------------------------------------------------------------------
    # Timeframes
    # ------------------------------------------------------------------

    @property
    def htf_minutes(self) -> int:
        with self._lock:
            return self._htf_minutes

    @htf_minutes.setter
    def htf_minutes(self, value: int) -> None:
        with self._lock:
            self._htf_minutes = max(1, int(value))

    @property
    def mtf_minutes(self) -> int:
        with self._lock:
            return self._mtf_minutes

    @mtf_minutes.setter
    def mtf_minutes(self, value: int) -> None:
        with self._lock:
            self._mtf_minutes = max(1, int(value))

    @property
    def ltf_minutes(self) -> int:
        with self._lock:
            return self._ltf_minutes

    @ltf_minutes.setter
    def ltf_minutes(self, value: int) -> None:
        with self._lock:
            self._ltf_minutes = max(1, int(value))

    # ------------------------------------------------------------------
    # Zone / slippage thresholds
    # ------------------------------------------------------------------

    @property
    def retest_zone_pct(self) -> float:
        with self._lock:
            return self._retest_zone_pct

    @retest_zone_pct.setter
    def retest_zone_pct(self, value: float) -> None:
        with self._lock:
            self._retest_zone_pct = max(0.01, min(5.0, float(value)))

    @property
    def slippage_pct(self) -> float:
        with self._lock:
            return self._slippage_pct

    @slippage_pct.setter
    def slippage_pct(self, value: float) -> None:
        with self._lock:
            self._slippage_pct = max(0.0, min(2.0, float(value)))

    # ------------------------------------------------------------------
    # Convenience snapshot (for logging / passing to backtest engine)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "htf_minutes":     self._htf_minutes,
                "mtf_minutes":     self._mtf_minutes,
                "ltf_minutes":     self._ltf_minutes,
                "retest_zone_pct": self._retest_zone_pct,
                "slippage_pct":    self._slippage_pct,
            }

    def apply_snapshot(self, d: dict) -> None:
        """Restore all params from a snapshot dict."""
        with self._lock:
            self._htf_minutes     = int(d.get("htf_minutes",     self._htf_minutes))
            self._mtf_minutes     = int(d.get("mtf_minutes",     self._mtf_minutes))
            self._ltf_minutes     = int(d.get("ltf_minutes",     self._ltf_minutes))
            self._retest_zone_pct = float(d.get("retest_zone_pct", self._retest_zone_pct))
            self._slippage_pct    = float(d.get("slippage_pct",    self._slippage_pct))


# ---------------------------------------------------------------------------
# Module-level singleton — import this from all other modules
# ---------------------------------------------------------------------------

_strategy_config = StrategyConfig()
