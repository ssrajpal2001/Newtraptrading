"""
app_ui.py
Streamlit-based institutional monitoring terminal.

Run with:
    streamlit run app_ui.py

Design language: Obsidian Dark Theme
  Canvas:        #0D1117
  Components:    #161B22
  Bullish / CE:  #238636
  Bearish / PE:  #DA3633
  Active Zone:   #58A6FF
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import UI_REFRESH_INTERVAL_SECS, is_market_open
from database import (
    ClientsRegistry,
    OptionType,
    TrapStatus,
    db_session,
    get_active_traps,
    get_all_active_clients,
    get_client_trades,
    init_db,
)
from execution_engine import engine
from bridge import _data_bridge

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="NewTrap Trading — Institutional Scanner",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Global CSS override (Obsidian theme)
# ---------------------------------------------------------------------------

DARK_CSS = """
<style>
  :root {
    --bg-canvas:    #0D1117;
    --bg-component: #161B22;
    --clr-bull:     #238636;
    --clr-bear:     #DA3633;
    --clr-active:   #58A6FF;
    --clr-text:     #C9D1D9;
    --clr-muted:    #8B949E;
    --clr-border:   #30363D;
  }
  html, body, [class*="css"] {
    background-color: var(--bg-canvas) !important;
    color: var(--clr-text) !important;
    font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
  }
  .stApp { background-color: var(--bg-canvas); }
  section[data-testid="stSidebar"] { background-color: var(--bg-component) !important; }

  /* Metric cards */
  div[data-testid="metric-container"] {
    background: var(--bg-component);
    border: 1px solid var(--clr-border);
    border-radius: 6px;
    padding: 12px;
  }

  /* Buttons */
  .stButton > button {
    background: var(--bg-component);
    color: var(--clr-text);
    border: 1px solid var(--clr-border);
    border-radius: 4px;
    transition: border-color 0.2s;
  }
  .stButton > button:hover { border-color: var(--clr-active); }

  /* Panic button special styling */
  .panic-btn > button {
    background: #6e1b1b !important;
    color: #FF6B6B !important;
    border: 2px solid var(--clr-bear) !important;
    font-size: 1.1rem !important;
    font-weight: bold !important;
    letter-spacing: 1px;
  }
  .panic-btn > button:hover { background: var(--clr-bear) !important; color: #fff !important; }

  .retest-flash {
    animation: flash 1s infinite;
    background: #2a1f00 !important;
    border: 2px solid #f0a500 !important;
    border-radius: 6px;
    padding: 8px 12px;
    font-weight: bold;
  }
  @keyframes flash {
    0%   { opacity: 1; }
    50%  { opacity: 0.4; }
    100% { opacity: 1; }
  }

  /* Table */
  .dataframe { background: var(--bg-component) !important; color: var(--clr-text) !important; }
  thead tr th { background: #0a0f14 !important; }

  /* Divider */
  hr { border-color: var(--clr-border); }
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------

def _init_state() -> None:
    defaults = {
        "day_config":      None,
        "ce_bars":         [],
        "pe_bars":         [],
        "active_traps":    [],
        "clients":         [],
        "retest_active":   False,
        "last_ce_price":   None,
        "last_pe_price":   None,
        "panic_confirmed": False,
        "log_lines":       [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()
init_db()


# ---------------------------------------------------------------------------
# Helper: build Plotly candlestick figure
# ---------------------------------------------------------------------------

def _build_candle_chart(
    bars: list,
    title: str,
    trap_levels: list,
    entry_line: Optional[float],
    sl_line: Optional[float],
    target_line: Optional[float],
    retest_zone_low: Optional[float],
    retest_zone_high: Optional[float],
    bull_color: str = "#238636",
    bear_color: str = "#DA3633",
) -> go.Figure:
    if not bars:
        fig = go.Figure()
        fig.update_layout(
            title=title,
            paper_bgcolor="#0D1117",
            plot_bgcolor="#0D1117",
            font=dict(color="#C9D1D9"),
        )
        return fig

    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close"])
    df["ts"] = pd.to_datetime(df["ts"])

    fig = go.Figure(data=[
        go.Candlestick(
            x=df["ts"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            increasing_line_color=bull_color,
            decreasing_line_color=bear_color,
            name="Premium",
        )
    ])

    # Active 75-min trap zones (shaded blue bands)
    for trap in trap_levels:
        origin = getattr(trap, "entry_origin", None)
        if origin is None:
            continue
        fig.add_hline(
            y=origin,
            line_dash="dash",
            line_color="#58A6FF",
            line_width=1.5,
            annotation_text=f"Trap Origin {origin:.1f}",
            annotation_font_color="#58A6FF",
        )

    # Retest zone shading (orange)
    if retest_zone_low and retest_zone_high:
        fig.add_hrect(
            y0=retest_zone_low, y1=retest_zone_high,
            fillcolor="#f0a500", opacity=0.08,
            line_width=0,
        )

    # Entry line (green)
    if entry_line:
        fig.add_hline(
            y=entry_line, line_color="#238636", line_width=2,
            annotation_text=f"Entry {entry_line:.1f}",
            annotation_font_color="#238636",
        )

    # SL line (red dashed)
    if sl_line:
        fig.add_hline(
            y=sl_line, line_dash="dash", line_color="#DA3633", line_width=1.5,
            annotation_text=f"SL {sl_line:.1f}",
            annotation_font_color="#DA3633",
        )

    # Target line (blue)
    if target_line:
        fig.add_hline(
            y=target_line, line_color="#58A6FF", line_width=1.5,
            annotation_text=f"Target {target_line:.1f}",
            annotation_font_color="#58A6FF",
        )

    fig.update_layout(
        title=dict(text=title, font=dict(color="#C9D1D9", size=13)),
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        font=dict(color="#C9D1D9", family="JetBrains Mono"),
        xaxis=dict(
            gridcolor="#21262D",
            showgrid=True,
            rangeslider=dict(visible=False),
            color="#8B949E",
        ),
        yaxis=dict(gridcolor="#21262D", showgrid=True, color="#8B949E"),
        margin=dict(l=40, r=20, t=40, b=20),
        height=360,
        showlegend=False,
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar: session info & engine controls
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    st.sidebar.markdown("## 🔧 Engine Controls")
    st.sidebar.markdown(
        f"**Date:** `{datetime.now().strftime('%Y-%m-%d')}`  \n"
        f"**Time:** `{datetime.now().strftime('%H:%M:%S')}`"
    )

    mkt_status = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    st.sidebar.markdown(f"**Market:** {mkt_status}")

    day_cfg = st.session_state.day_config
    if day_cfg:
        st.sidebar.markdown("---")
        st.sidebar.markdown("### Morning Anchor")
        st.sidebar.markdown(
            f"**Center Point:** `{day_cfg.center_point:.2f}`  \n"
            f"**ITM Offset:**   `±{day_cfg.itm_offset} pts`  \n"
            f"**CE Strike:**    `{day_cfg.ce_strike}`  \n"
            f"**PE Strike:**    `{day_cfg.pe_strike}`"
        )
        st.sidebar.markdown(
            f"**CE Symbol:** `{day_cfg.ce_symbol}`  \n"
            f"**PE Symbol:** `{day_cfg.pe_symbol}`"
        )

    st.sidebar.markdown("---")
    st.sidebar.markdown("### Active Traps")
    traps = st.session_state.active_traps
    if traps:
        for t in traps:
            color = "#238636" if t.option_type == OptionType.CE else "#DA3633"
            st.sidebar.markdown(
                f"<span style='color:{color}'>■</span> "
                f"**{t.option_type}** {t.strike} | origin `{t.entry_origin:.1f}` | "
                f"target `{t.target_high:.1f}`",
                unsafe_allow_html=True,
            )
    else:
        st.sidebar.markdown("_No active traps_")

    st.sidebar.markdown("---")
    if st.sidebar.button("🔄 Refresh Clients"):
        engine.reload_clients()
        st.session_state.clients = get_all_active_clients()
        st.sidebar.success("Clients reloaded")


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------

def _render_main() -> None:
    st.markdown(
        "<h1 style='color:#58A6FF; letter-spacing:2px; font-size:1.6rem;'>"
        "NEWTRAP — INSTITUTIONAL LIQUIDITY SCANNER</h1>",
        unsafe_allow_html=True,
    )

    # ---- Retest flash banner ----
    if st.session_state.retest_active:
        st.markdown(
            "<div class='retest-flash'>⚠️  RETEST SEQUENCE ACTIVE — "
            "Premium inside 75-Min Trap Zone. Monitoring 5-Min structure …</div>",
            unsafe_allow_html=True,
        )

    # ---- Top metrics row ----
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("CE Last Price", f"₹{st.session_state.last_ce_price or '—'}")
    col2.metric("PE Last Price", f"₹{st.session_state.last_pe_price or '—'}")
    col3.metric("Active Traps",  len(st.session_state.active_traps))
    col4.metric("Active Clients", len(st.session_state.clients))
    day_cfg = st.session_state.day_config
    col5.metric(
        "Center Point",
        f"{day_cfg.center_point:.0f}" if day_cfg else "—",
    )

    st.markdown("---")

    # ---- Charts ----
    ce_traps = [t for t in st.session_state.active_traps if t.option_type == OptionType.CE]
    pe_traps = [t for t in st.session_state.active_traps if t.option_type == OptionType.PE]

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        day_cfg = st.session_state.day_config
        title   = f"CE Contract — {day_cfg.ce_symbol if day_cfg else 'CE'} (75-Min HTF)"
        fig     = _build_candle_chart(
            bars=st.session_state.ce_bars,
            title=title,
            trap_levels=ce_traps,
            entry_line=None,
            sl_line=None,
            target_line=ce_traps[0].target_high if ce_traps else None,
            retest_zone_low=ce_traps[0].entry_origin * 0.995 if ce_traps else None,
            retest_zone_high=ce_traps[0].entry_origin * 1.005 if ce_traps else None,
            bull_color="#238636",
            bear_color="#DA3633",
        )
        st.plotly_chart(fig, use_container_width=True, key="ce_chart")

    with chart_col2:
        title = f"PE Contract — {day_cfg.pe_symbol if day_cfg else 'PE'} (75-Min HTF)"
        fig   = _build_candle_chart(
            bars=st.session_state.pe_bars,
            title=title,
            trap_levels=pe_traps,
            entry_line=None,
            sl_line=None,
            target_line=pe_traps[0].target_high if pe_traps else None,
            retest_zone_low=pe_traps[0].entry_origin * 0.995 if pe_traps else None,
            retest_zone_high=pe_traps[0].entry_origin * 1.005 if pe_traps else None,
            bull_color="#238636",
            bear_color="#DA3633",
        )
        st.plotly_chart(fig, use_container_width=True, key="pe_chart")

    st.markdown("---")

    # ---- Trap registry table ----
    st.markdown("### 📋 Historical Trap Registry")
    _render_trap_table()

    st.markdown("---")

    # ---- Client dashboard ----
    st.markdown("### 👥 Client Portfolio Dashboard")
    _render_client_dashboard()

    st.markdown("---")

    # ---- System log ----
    st.markdown("### 🖥️ System Log")
    log_text = "\n".join(st.session_state.log_lines[-40:])
    st.code(log_text if log_text else "No log entries yet.", language="")


def _render_trap_table() -> None:
    with db_session() as s:
        from database import HistoricalOptionTraps
        rows = s.query(HistoricalOptionTraps).order_by(
            HistoricalOptionTraps.id.desc()
        ).limit(30).all()
        data = [
            {
                "ID":       r.id,
                "Date":     r.trade_date.strftime("%Y-%m-%d %H:%M") if r.trade_date else "",
                "Type":     r.option_type.value if r.option_type else "",
                "Strike":   r.strike,
                "Symbol":   r.contract_symbol,
                "Origin":   f"{r.entry_origin:.2f}",
                "Target":   f"{r.target_high:.2f}",
                "SL":       f"{r.candle_low_sl:.2f}" if r.candle_low_sl else "—",
                "Status":   r.status.value if r.status else "",
            }
            for r in rows
        ]
        for r in rows:
            s.expunge(r)

    if data:
        df = pd.DataFrame(data)

        def _color_status(val: str) -> str:
            colors = {
                "ACTIVE_UNMITIGATED": "color: #58A6FF",
                "MITIGATED":          "color: #238636",
                "VOIDED":             "color: #DA3633",
                "EXPIRED_VOID":       "color: #8B949E",
            }
            return colors.get(val, "")

        styled = df.style.applymap(_color_status, subset=["Status"])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("No traps recorded yet.")


def _render_client_dashboard() -> None:
    clients = st.session_state.clients
    if not clients:
        clients = get_all_active_clients()
        st.session_state.clients = clients

    if not clients:
        st.warning("No active clients configured in the database.")
        _render_panic_button()
        return

    open_pos = engine.get_open_positions()

    cols = st.columns(min(len(clients), 4))
    for idx, client in enumerate(clients):
        col = cols[idx % len(cols)]
        pos = open_pos.get(client.id)
        with col:
            st.markdown(
                f"<div style='background:#161B22; border:1px solid #30363D; "
                f"border-radius:6px; padding:12px; margin-bottom:8px;'>"
                f"<b style='color:#58A6FF;'>{client.name}</b><br>"
                f"<span style='color:#8B949E; font-size:0.8rem;'>{client.broker}</span><br>"
                f"<hr style='border-color:#30363D; margin:6px 0;'>"
                f"<b>Status:</b> {'🟢 ACTIVE' if client.active else '🔴 INACTIVE'}<br>"
                f"<b>Capital:</b> ₹{client.max_capital:,.0f}<br>"
                f"<b>Position:</b> {'🔵 ' + pos[1] if pos else 'None'}"
                f"</div>",
                unsafe_allow_html=True,
            )

    _render_panic_button()


def _render_panic_button() -> None:
    st.markdown("---")
    st.markdown(
        "<h3 style='color:#DA3633; letter-spacing:2px;'>⚡ GLOBAL MASTER PANIC</h3>",
        unsafe_allow_html=True,
    )

    col_a, col_b, _ = st.columns([1, 1, 4])

    with col_a:
        st.markdown('<div class="panic-btn">', unsafe_allow_html=True)
        if st.button("🚨 SQUARE OFF ALL", key="panic_btn"):
            st.session_state.panic_confirmed = True
        st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.panic_confirmed:
        with col_b:
            st.markdown(
                "<span style='color:#f0a500; font-weight:bold;'>Confirm?</span>",
                unsafe_allow_html=True,
            )
            if st.button("✅ YES, CLOSE ALL", key="panic_confirm"):
                _run_async(engine.panic_square_off_all())
                st.session_state.panic_confirmed = False
                st.success("All positions closed.")
            if st.button("❌ Cancel", key="panic_cancel"):
                st.session_state.panic_confirmed = False


# ---------------------------------------------------------------------------
# Async runner shim (Streamlit runs in a sync context)
# ---------------------------------------------------------------------------

def _run_async(coro) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.result(timeout=10)
        else:
            loop.run_until_complete(coro)
    except Exception as exc:
        logger.exception("Async runner error: %s", exc)
        st.error(f"Execution error: {exc}")


# ---------------------------------------------------------------------------
# Live data refresh callback (called periodically via st.rerun)
# ---------------------------------------------------------------------------

def _refresh_live_data() -> None:
    st.session_state.active_traps = get_active_traps()
    st.session_state.clients      = get_all_active_clients()

    # Pull latest bar data from the shared aggregator state if available
    from data_feeder import TICK_QUEUE  # noqa: F401
    # Bar data is injected into session_state by the background thread
    # (see StreamlitDataBridge below)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()
    _refresh_live_data()
    _data_bridge.flush_to_session()
    _render_main()

    # Auto-rerun every N seconds for live feel
    st.markdown(
        f"<meta http-equiv='refresh' content='{UI_REFRESH_INTERVAL_SECS}'>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
