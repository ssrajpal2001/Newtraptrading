"""
bridge.py
Thread-safe data bridge between the async engine (BarAggregator / TrapDetector)
and the Streamlit UI render loop.

The engine runs in a separate asyncio thread; Streamlit rerenders on a timer.
This module provides a lock-protected buffer that the engine writes to and the
UI reads from via flush_to_session().

Import the singleton:
    from bridge import _data_bridge
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Nifty spot index symbols (used by DualFeederSupervisor & TrapDetector ATM)
# ---------------------------------------------------------------------------

# Upstox V3 instrument key for the Nifty 50 index
NIFTY_SPOT_DISPLAY   = "NSE_INDEX|Nifty 50"
# Fyers symbol for the Nifty 50 index (used in Fyers WebSocket subscription)
NIFTY_SPOT_FYERS     = "NSE:NIFTY50-INDEX"


# ---------------------------------------------------------------------------
# Bridge class
# ---------------------------------------------------------------------------

class StreamlitDataBridge:
    """
    Runs shared between the async engine thread and the Streamlit render thread.
    All public methods are thread-safe via a single reentrant lock.
    """

    def __init__(self) -> None:
        self._lock       = threading.Lock()
        self._ce_bars:   List[Tuple] = []
        self._pe_bars:   List[Tuple] = []
        self._last_ce_price:  Optional[float] = None
        self._last_pe_price:  Optional[float] = None
        self._symbol_prices:  Dict[str, float] = {}   # symbol → last tick price
        self._spot_price:     Optional[float] = None
        self._retest_active:  bool            = False
        self._active_traps:   List[Any]       = []

    # ------------------------------------------------------------------
    # Bar data (called from BarAggregator on HTF bar close)
    # ------------------------------------------------------------------

    def push_bar(self, symbol: str, bar_tuple: Tuple) -> None:
        """Append a completed OHLCV bar tuple (ts, open, high, low, close)."""
        with self._lock:
            if "CE" in symbol:
                self._ce_bars.append(bar_tuple)
                if len(self._ce_bars) > 500:
                    self._ce_bars = self._ce_bars[-500:]
            else:
                self._pe_bars.append(bar_tuple)
                if len(self._pe_bars) > 500:
                    self._pe_bars = self._pe_bars[-500:]

    # ------------------------------------------------------------------
    # Live tick prices (called from BarAggregator on every tick)
    # ------------------------------------------------------------------

    def push_tick(self, symbol: str, price: float) -> None:
        """Update the latest CE or PE option premium price."""
        with self._lock:
            self._symbol_prices[symbol] = price
            if "CE" in symbol:
                self._last_ce_price = price
            else:
                self._last_pe_price = price

    def get_last_price(self, symbol: str) -> Optional[float]:
        """
        Return the most recent tick price for an option symbol.
        Falls back to the tracked CE/PE price if the exact symbol has no entry yet
        (handles the case where the executed ATM symbol differs from the tracked ITM symbol).
        """
        with self._lock:
            exact = self._symbol_prices.get(symbol)
            if exact is not None:
                return exact
            # Fallback by option type
            return self._last_ce_price if "CE" in symbol else self._last_pe_price

    # ------------------------------------------------------------------
    # Nifty spot index price (called from _decode_upstox_binary for index feed)
    # ------------------------------------------------------------------

    def update_spot_price(self, price: float) -> None:
        with self._lock:
            self._spot_price = price

    def get_spot_price(self) -> Optional[float]:
        with self._lock:
            return self._spot_price

    # ------------------------------------------------------------------
    # Retest zone flag (called from TrapDetector.on_ltf_bar_close)
    # ------------------------------------------------------------------

    def set_retest_active(self, active: bool) -> None:
        with self._lock:
            self._retest_active = active

    # ------------------------------------------------------------------
    # Flush snapshot to Streamlit session_state (called from UI render loop)
    # ------------------------------------------------------------------

    def flush_to_session(self) -> None:
        """Copy buffered data into st.session_state for Streamlit rendering."""
        import streamlit as st  # deferred import — only valid inside Streamlit context
        with self._lock:
            st.session_state.ce_bars       = list(self._ce_bars)
            st.session_state.pe_bars       = list(self._pe_bars)
            st.session_state.retest_active = self._retest_active
            if self._last_ce_price is not None:
                st.session_state.last_ce_price = self._last_ce_price
            if self._last_pe_price is not None:
                st.session_state.last_pe_price = self._last_pe_price


# ---------------------------------------------------------------------------
# Module-level singleton — import this from all other modules
# ---------------------------------------------------------------------------

_data_bridge = StreamlitDataBridge()
