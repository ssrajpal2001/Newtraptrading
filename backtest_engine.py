"""
backtest_engine.py
Dynamic Multi-Timeframe Backtest Engine.

Loads stored 1-minute bars from option_1m_bar_repository, resamples them
into the configured HTF and MTF bar series using Pandas, and replays the
institutional seller-trap strategy deterministically.

Architecture
------------
  load_1m_bars(DB)  →  Pandas resample  →  in-memory TrapStateMachine
                                        →  BacktestResult list
                                        →  trades_ledger (is_backtest=True)

Constraints:
  - NEVER accesses Nifty Spot or Futures data.
  - All analysis runs purely on the stored option premium bars.
  - Backtest trades are written with is_backtest=True so live P&L is unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import pandas as pd

from database import (
    ExitCategory,
    Option1mBar,
    TradesLedger,
    db_session,
    load_1m_bars,
    record_trade_entry,
    record_trade_exit,
)
from strategy_config import StrategyConfig, _strategy_config

logger = logging.getLogger(__name__)

# Sentinel client_id used for all backtest trades
_BACKTEST_CLIENT_ID = -1


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    symbol:        str
    entry_price:   float
    exit_price:    Optional[float]
    quantity:      int
    pnl:           Optional[float]
    exit_category: Optional[str]
    entered_at:    datetime
    exited_at:     Optional[datetime]
    notes:         str = ""


@dataclass
class BacktestResult:
    symbol:           str
    from_dt:          datetime
    to_dt:            datetime
    strategy_params:  dict = field(default_factory=dict)
    trades:           List[BacktestTrade] = field(default_factory=list)
    log_lines:        List[str]           = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def closed_trades(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.exit_price is not None]

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl or 0.0 for t in self.closed_trades)

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        wins = [t for t in closed if (t.pnl or 0) > 0]
        return len(wins) / len(closed) * 100


# ---------------------------------------------------------------------------
# In-memory trap state machine (no DB side-effects for active trap state)
# ---------------------------------------------------------------------------

class _TrapState:
    """Lightweight per-symbol state machine that mirrors TrapDetector logic."""

    def __init__(self, retest_zone_pct: float) -> None:
        self._zone_pct           = retest_zone_pct / 100.0
        # HTF state
        self._htf_bearish_high:  Optional[float] = None
        self._htf_bearish_open:  Optional[float] = None
        self._htf_target:        Optional[float] = None
        self._htf_confirmed:     bool            = False
        # LTF state
        self._in_retest:         bool            = False
        self._ltf_bearish_high:  Optional[float] = None
        self._ltf_bearish_open:  Optional[float] = None
        self._ltf_confirmed:     bool            = False
        self._entry_line:        Optional[float] = None
        self._sl_line:           Optional[float] = None
        # Position state
        self._in_trade:          bool            = False
        self._trade_sl:          Optional[float] = None
        self._trade_target:      Optional[float] = None
        self._trade_entry_price: Optional[float] = None
        self._trade_entry_time:  Optional[datetime] = None

    def on_htf_bar(self, bar: pd.Series) -> Optional[str]:
        """Process one HTF bar. Returns 'trap_confirmed' or None."""
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

        if not self._htf_confirmed:
            if self._htf_bearish_high is None:
                if c < o:  # bearish candle
                    self._htf_bearish_high = h
                    self._htf_bearish_open = o
            else:
                if h > self._htf_bearish_high:
                    # Sellers trapped — confirm HTF trap
                    self._htf_confirmed = True
                    self._htf_target    = h
                    return "trap_confirmed"
                if c < o:  # rolling update
                    self._htf_bearish_high = h
                    self._htf_bearish_open = o
        return None

    def on_mtf_bar(self, bar: pd.Series) -> Optional[str]:
        """Process one MTF bar. Returns 'entry_ready' or None."""
        if not self._htf_confirmed or self._in_trade:
            return None

        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        origin = self._htf_bearish_open or 0.0

        if not self._in_retest:
            zone_hi = origin * (1.0 + self._zone_pct)
            zone_lo = origin * (1.0 - self._zone_pct)
            if zone_lo <= l <= zone_hi or zone_lo <= c <= zone_hi:
                self._in_retest = True
            return None

        # In retest zone — watch for nested 5-min seller trap
        if self._ltf_bearish_high is None:
            if c < o:
                self._ltf_bearish_high = h
                self._ltf_bearish_open = o
        elif h > self._ltf_bearish_high:
            self._ltf_confirmed = True
            self._entry_line    = self._ltf_bearish_open
            self._sl_line       = min(l, bar["low"])   # trap candle low
            return "entry_ready"
        elif c < o:
            self._ltf_bearish_high = h
            self._ltf_bearish_open = o

        return None

    def check_tick_entry(self, price: float, ts: datetime) -> bool:
        """Return True if price touches entry line and we should open position."""
        if (
            self._ltf_confirmed
            and not self._in_trade
            and self._entry_line is not None
            and price <= self._entry_line
        ):
            self._in_trade          = True
            self._trade_sl          = self._sl_line
            self._trade_target      = self._htf_target
            self._trade_entry_price = price
            self._trade_entry_time  = ts
            return True
        return False

    def check_ltf_exit(self, bar: pd.Series) -> Optional[str]:
        """Check 1-min close SL. Returns 'sl_hit' or None."""
        if self._in_trade and self._trade_sl and bar["close"] < self._trade_sl:
            return "sl_hit"
        return None

    def check_target_exit(self, price: float) -> Optional[str]:
        """Check if price has reached the 75-min trapped candle high target."""
        if self._in_trade and self._trade_target and price >= self._trade_target:
            return "target_hit"
        return None

    def reset_trade(self) -> None:
        self._in_retest         = False
        self._ltf_bearish_high  = None
        self._ltf_bearish_open  = None
        self._ltf_confirmed     = False
        self._entry_line        = None
        self._sl_line           = None
        self._in_trade          = False
        self._trade_sl          = None
        self._trade_target      = None
        self._trade_entry_price = None
        self._trade_entry_time  = None
        # Also reset HTF state for next fresh setup
        self._htf_confirmed     = False
        self._htf_bearish_high  = None
        self._htf_bearish_open  = None
        self._htf_target        = None


# ---------------------------------------------------------------------------
# Pandas resampling helpers
# ---------------------------------------------------------------------------

_RESAMPLE_AGG = {
    "open":   "first",
    "high":   "max",
    "low":    "min",
    "close":  "last",
    "volume": "sum",
}


def _bars_to_df(bars: List[Option1mBar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    records = [
        {
            "timestamp": b.timestamp,
            "open":      b.open,
            "high":      b.high,
            "low":       b.low,
            "close":     b.close,
            "volume":    b.volume,
        }
        for b in bars
    ]
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    return df


def _resample(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if df_1m.empty:
        return df_1m
    resampled = df_1m.resample(f"{minutes}min").agg(_RESAMPLE_AGG).dropna()
    return resampled


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class DynamicBacktestEngine:
    """
    Replays stored 1-minute option bars through the institutional trap strategy.

    Usage:
        engine = DynamicBacktestEngine()
        result = engine.run(
            symbol="NSE:NIFTY03JUN2523500CE",
            from_dt=datetime(2025, 6, 2),
            to_dt=datetime(2025, 6, 3),
            cfg=_strategy_config,            # or a custom StrategyConfig
            quantity=25,                     # lots * LOT_SIZE
            persist_trades=True,
        )
    """

    def run(
        self,
        symbol: str,
        from_dt: datetime,
        to_dt: datetime,
        cfg: Optional[StrategyConfig] = None,
        quantity: int = 25,
        persist_trades: bool = True,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> BacktestResult:
        cfg = cfg or _strategy_config
        params = cfg.snapshot()

        result = BacktestResult(
            symbol=symbol,
            from_dt=from_dt,
            to_dt=to_dt,
            strategy_params=params,
        )

        def _log(msg: str) -> None:
            result.log_lines.append(msg)
            logger.info("[Backtest] %s", msg)
            if log_cb:
                log_cb(msg)

        _log(f"Starting backtest | symbol={symbol} "
             f"HTF={params['htf_minutes']}m MTF={params['mtf_minutes']}m "
             f"LTF={params['ltf_minutes']}m "
             f"zone={params['retest_zone_pct']}% qty={quantity}")

        # 1. Load 1m bars from DB
        raw_bars = load_1m_bars(symbol, from_dt, to_dt)
        if not raw_bars:
            _log(f"No 1m bars found for {symbol} between {from_dt} and {to_dt}.")
            _log("Run the live engine during market hours to build the data vault.")
            return result

        _log(f"Loaded {len(raw_bars)} 1m bars from vault.")

        df_1m = _bars_to_df(raw_bars)

        # 2. Resample into HTF and MTF series
        df_htf = _resample(df_1m, params["htf_minutes"])
        df_mtf = _resample(df_1m, params["mtf_minutes"])

        _log(f"Resampled → {len(df_htf)} HTF bars, {len(df_mtf)} MTF bars.")

        # 3. Replay through state machine
        state = _TrapState(retest_zone_pct=params["retest_zone_pct"])
        active_trade: Optional[BacktestTrade] = None
        active_trade_id: Optional[int] = None

        # Merge all bar timestamps into a sorted timeline
        all_ts = sorted(set(df_htf.index) | set(df_mtf.index) | set(df_1m.index))

        for ts in all_ts:
            # --- HTF bar boundary ---
            if ts in df_htf.index:
                bar = df_htf.loc[ts]
                event = state.on_htf_bar(bar)
                if event == "trap_confirmed":
                    _log(f"{ts} | HTF trap confirmed — origin≈{state._htf_bearish_open:.2f} "
                         f"target={state._htf_target:.2f}")

            # --- MTF bar boundary ---
            if ts in df_mtf.index and active_trade is None:
                bar = df_mtf.loc[ts]
                event = state.on_mtf_bar(bar)
                if event == "entry_ready":
                    _log(f"{ts} | MTF entry zone ready — "
                         f"entry_line={state._entry_line:.2f} sl={state._sl_line:.2f}")

            # --- 1m bar (risk / SL monitoring + tick entry simulation) ---
            if ts in df_1m.index:
                bar_1m = df_1m.loc[ts]
                close  = float(bar_1m["close"])
                low    = float(bar_1m["low"])
                high   = float(bar_1m["high"])

                # Check touch entry on bar close (conservative — avoids look-ahead)
                if active_trade is None and state.check_tick_entry(close, ts):
                    entry_px = close
                    active_trade = BacktestTrade(
                        symbol=symbol,
                        entry_price=entry_px,
                        exit_price=None,
                        quantity=quantity,
                        pnl=None,
                        exit_category=None,
                        entered_at=ts,
                        exited_at=None,
                    )
                    _log(f"{ts} | ENTRY @ ₹{entry_px:.2f} (qty={quantity})")

                    if persist_trades:
                        trade_row = record_trade_entry(
                            client_id=_BACKTEST_CLIENT_ID,
                            contract_symbol=symbol,
                            entry_price=entry_px,
                            quantity=quantity,
                            is_backtest=True,
                        )
                        active_trade_id = trade_row.id

                    continue   # don't check exit on same bar as entry

                if active_trade is not None:
                    # Check 1m close-based SL
                    sl_event = state.check_ltf_exit(bar_1m)
                    tgt_event = state.check_target_exit(high)

                    exit_cat = None
                    exit_px  = None

                    if sl_event == "sl_hit":
                        exit_px  = float(state._trade_sl or close)
                        exit_cat = ExitCategory.SL_HIT
                    elif tgt_event == "target_hit":
                        exit_px  = float(state._trade_target or high)
                        exit_cat = ExitCategory.TARGET_HIT

                    if exit_cat and exit_px is not None:
                        pnl = (exit_px - active_trade.entry_price) * active_trade.quantity
                        active_trade.exit_price    = exit_px
                        active_trade.pnl           = pnl
                        active_trade.exit_category = exit_cat.value
                        active_trade.exited_at     = ts
                        result.trades.append(active_trade)

                        _log(
                            f"{ts} | EXIT {exit_cat.value} @ ₹{exit_px:.2f} "
                            f"P&L=₹{pnl:+,.2f}"
                        )

                        if persist_trades and active_trade_id is not None:
                            record_trade_exit(active_trade_id, exit_px, exit_cat)

                        active_trade    = None
                        active_trade_id = None
                        state.reset_trade()

        # Force-close any open position at end of replay window
        if active_trade is not None:
            last_close = float(df_1m.iloc[-1]["close"]) if not df_1m.empty else active_trade.entry_price
            pnl = (last_close - active_trade.entry_price) * active_trade.quantity
            active_trade.exit_price    = last_close
            active_trade.pnl           = pnl
            active_trade.exit_category = ExitCategory.EXPIRY_VOID.value
            active_trade.exited_at     = to_dt
            result.trades.append(active_trade)

            _log(f"End of replay | force-close @ ₹{last_close:.2f} P&L=₹{pnl:+,.2f}")

            if persist_trades and active_trade_id is not None:
                record_trade_exit(active_trade_id, last_close, ExitCategory.EXPIRY_VOID)

        _log(
            f"Backtest complete | trades={result.total_trades} "
            f"net_pnl=₹{result.net_pnl:+,.2f} win_rate={result.win_rate:.1f}%"
        )
        return result
