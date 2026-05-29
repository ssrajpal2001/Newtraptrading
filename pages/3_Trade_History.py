"""
pages/3_Trade_History.py
Dedicated trade history and P&L analytics workspace.

Features
--------
  • Date range filter
  • Per-client filter (multi-select)
  • Summary panel: total trades, win rate, average P&L, largest win/loss
  • Interactive Plotly cumulative P&L equity curve
  • Full trade table with colour-coded P&L column
  • CSV export button
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import and_

from database import (
    ClientsRegistry,
    ExitCategory,
    TradesLedger,
    db_session,
    get_all_active_clients,
    init_db,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Trade History — NewTrap",
    page_icon="📊",
    layout="wide",
)

DARK_CSS = """
<style>
  html, body, [class*="css"] {
    background-color: #0D1117 !important;
    color: #C9D1D9 !important;
    font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
  }
  .stDataFrame, .stTable { background-color: #161B22 !important; }
  .metric-card {
    background: #161B22;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 16px 20px;
    text-align: center;
  }
  .metric-label { color: #8B949E; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; }
  .metric-value { color: #C9D1D9; font-size: 1.6rem; font-weight: 700; margin-top: 4px; }
  .metric-value.positive { color: #238636; }
  .metric-value.negative { color: #DA3633; }
  [data-testid="stSidebar"] { background-color: #161B22 !important; }
  .stSelectbox, .stMultiSelect { background-color: #161B22 !important; }
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# DB init guard
# ---------------------------------------------------------------------------
init_db()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXIT_LABEL = {
    ExitCategory.TARGET_HIT.value:    "Target Hit",
    ExitCategory.SL_HIT.value:        "SL Hit",
    ExitCategory.MANUAL_SQUARE.value: "Manual",
    ExitCategory.EXPIRY_VOID.value:   "Expiry Void",
    None:                             "Open",
}

EXIT_COLOUR = {
    "Target Hit":  "#238636",
    "SL Hit":      "#DA3633",
    "Manual":      "#58A6FF",
    "Expiry Void": "#8B949E",
    "Open":        "#f0a500",
}


def _load_trades(
    from_date: date,
    to_date: date,
    client_ids: list[int],
) -> pd.DataFrame:
    """Query trades_ledger and return a tidy DataFrame."""
    from_dt = datetime(from_date.year, from_date.month, from_date.day, 0, 0, 0)
    to_dt   = datetime(to_date.year,   to_date.month,   to_date.day,   23, 59, 59)

    with db_session() as s:
        q = s.query(TradesLedger, ClientsRegistry.name).join(
            ClientsRegistry, TradesLedger.client_id == ClientsRegistry.id
        ).filter(
            and_(
                TradesLedger.entered_at >= from_dt,
                TradesLedger.entered_at <= to_dt,
            )
        )
        if client_ids:
            q = q.filter(TradesLedger.client_id.in_(client_ids))

        rows = q.order_by(TradesLedger.entered_at.asc()).all()

        records = []
        for trade, client_name in rows:
            records.append({
                "id":              trade.id,
                "client":          client_name,
                "symbol":          trade.contract_symbol,
                "entry_price":     trade.entry_price,
                "exit_price":      trade.exit_price,
                "quantity":        trade.quantity,
                "pnl":             trade.pnl,
                "exit_category":   EXIT_LABEL.get(
                    trade.exit_category.value if trade.exit_category else None,
                    "Open",
                ),
                "entered_at":      trade.entered_at,
                "exited_at":       trade.exited_at,
            })

    return pd.DataFrame(records)


def _summary_metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "total_trades": 0,
            "closed_trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "avg_pnl": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
        }

    closed = df[df["pnl"].notna()]
    wins   = closed[closed["pnl"] > 0]

    return {
        "total_trades":  len(df),
        "closed_trades": len(closed),
        "win_rate":      (len(wins) / len(closed) * 100) if len(closed) > 0 else 0.0,
        "net_pnl":       closed["pnl"].sum(),
        "avg_pnl":       closed["pnl"].mean() if len(closed) > 0 else 0.0,
        "largest_win":   closed["pnl"].max() if len(closed) > 0 else 0.0,
        "largest_loss":  closed["pnl"].min() if len(closed) > 0 else 0.0,
    }


def _equity_curve_chart(df: pd.DataFrame) -> go.Figure:
    """Cumulative P&L equity curve using closed trades sorted by exit time."""
    closed = df[df["pnl"].notna()].copy()
    closed["exited_at"] = pd.to_datetime(closed["exited_at"])
    closed = closed.sort_values("exited_at")
    closed["cumulative_pnl"] = closed["pnl"].cumsum()

    fig = go.Figure()

    if not closed.empty:
        fig.add_trace(go.Scatter(
            x=closed["exited_at"],
            y=closed["cumulative_pnl"],
            mode="lines+markers",
            name="Cumulative P&L",
            line=dict(color="#58A6FF", width=2),
            marker=dict(
                color=[
                    "#238636" if v >= 0 else "#DA3633"
                    for v in closed["pnl"]
                ],
                size=6,
            ),
            hovertemplate=(
                "<b>%{x|%d %b %Y %H:%M}</b><br>"
                "Cumulative P&L: ₹%{y:,.2f}<extra></extra>"
            ),
        ))

        # Zero line
        fig.add_hline(
            y=0,
            line_color="#30363D",
            line_width=1,
            line_dash="dash",
        )

        # Colour fill: green above zero, red below
        fig.add_trace(go.Scatter(
            x=closed["exited_at"],
            y=closed["cumulative_pnl"].clip(lower=0),
            fill="tozeroy",
            fillcolor="rgba(35,134,54,0.12)",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=closed["exited_at"],
            y=closed["cumulative_pnl"].clip(upper=0),
            fill="tozeroy",
            fillcolor="rgba(218,54,51,0.12)",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0D1117",
        plot_bgcolor="#161B22",
        font=dict(color="#C9D1D9", size=12),
        margin=dict(l=60, r=20, t=40, b=40),
        height=380,
        title=dict(
            text="Cumulative P&L Equity Curve",
            font=dict(color="#C9D1D9", size=14),
        ),
        xaxis=dict(
            gridcolor="#21262D",
            showgrid=True,
            title="Exit Time",
        ),
        yaxis=dict(
            gridcolor="#21262D",
            showgrid=True,
            title="Cumulative P&L (₹)",
            tickformat="₹,.0f",
        ),
    )
    return fig


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

st.markdown("## 📊 Trade History")
st.markdown(
    "<p style='color:#8B949E;'>Historical trade audit, P&amp;L analysis, "
    "and equity curve for all registered clients.</p>",
    unsafe_allow_html=True,
)
st.divider()

# ---- Filters ----
filter_col1, filter_col2, filter_col3 = st.columns([1.2, 1.2, 2])

with filter_col1:
    from_date = st.date_input(
        "From",
        value=date.today() - timedelta(days=30),
        max_value=date.today(),
    )

with filter_col2:
    to_date = st.date_input(
        "To",
        value=date.today(),
        min_value=from_date,
        max_value=date.today(),
    )

# Load all clients for filter dropdown
all_clients = get_all_active_clients()
client_options = {c.name: c.id for c in all_clients}

with filter_col3:
    selected_names = st.multiselect(
        "Clients",
        options=list(client_options.keys()),
        default=[],
        placeholder="All clients",
    )
    selected_ids = [client_options[n] for n in selected_names]

df = _load_trades(from_date, to_date, selected_ids)

st.markdown(
    f"<p style='color:#8B949E; font-size:0.85rem;'>"
    f"{len(df)} trade(s) found for the selected period.</p>",
    unsafe_allow_html=True,
)

st.divider()

# ---- Summary metrics ----
m = _summary_metrics(df)

mc1, mc2, mc3, mc4, mc5, mc6 = st.columns(6)

def _metric(col, label: str, value: str, css_class: str = "") -> None:
    col.markdown(
        f"<div class='metric-card'>"
        f"<div class='metric-label'>{label}</div>"
        f"<div class='metric-value {css_class}'>{value}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

_metric(mc1, "Total Trades",  str(m["total_trades"]))
_metric(mc2, "Closed",        str(m["closed_trades"]))
_metric(mc3, "Win Rate",      f"{m['win_rate']:.1f}%",
        "positive" if m["win_rate"] >= 50 else "negative")
_metric(mc4, "Net P&L",       f"₹{m['net_pnl']:,.0f}",
        "positive" if m["net_pnl"] >= 0 else "negative")
_metric(mc5, "Largest Win",   f"₹{m['largest_win']:,.0f}", "positive")
_metric(mc6, "Largest Loss",  f"₹{m['largest_loss']:,.0f}", "negative")

st.markdown("<br>", unsafe_allow_html=True)

# ---- Equity curve ----
if not df[df["pnl"].notna()].empty:
    fig = _equity_curve_chart(df)
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No closed trades in the selected period — equity curve unavailable.")

st.divider()

# ---- Trade table ----
st.markdown("#### Trade Log")

if df.empty:
    st.info("No trades found for the selected filters.")
else:
    display_df = df[[
        "id", "client", "symbol", "entry_price", "exit_price",
        "quantity", "pnl", "exit_category", "entered_at", "exited_at",
    ]].copy()

    display_df.rename(columns={
        "id":             "Trade ID",
        "client":         "Client",
        "symbol":         "Contract",
        "entry_price":    "Entry ₹",
        "exit_price":     "Exit ₹",
        "quantity":       "Qty",
        "pnl":            "P&L ₹",
        "exit_category":  "Exit Type",
        "entered_at":     "Entry Time",
        "exited_at":      "Exit Time",
    }, inplace=True)

    # Format timestamps
    for col in ("Entry Time", "Exit Time"):
        display_df[col] = pd.to_datetime(display_df[col]).dt.strftime("%d %b %Y %H:%M")

    # Colour P&L column
    def _colour_pnl(val):
        if pd.isna(val):
            return "color: #f0a500"
        return "color: #238636" if val >= 0 else "color: #DA3633"

    styled = (
        display_df.style
        .applymap(_colour_pnl, subset=["P&L ₹"])
        .format({
            "Entry ₹": "₹{:.2f}",
            "Exit ₹":  lambda v: f"₹{v:.2f}" if pd.notna(v) else "—",
            "P&L ₹":   lambda v: f"₹{v:,.2f}" if pd.notna(v) else "Open",
        })
        .set_properties(**{"background-color": "#161B22", "color": "#C9D1D9"})
        .set_table_styles([
            {"selector": "th", "props": [
                ("background-color", "#0D1117"),
                ("color", "#8B949E"),
                ("font-size", "0.75rem"),
                ("text-transform", "uppercase"),
                ("letter-spacing", "0.05em"),
            ]},
        ])
    )
    st.dataframe(styled, use_container_width=True, height=400)

    # ---- CSV export ----
    st.markdown("<br>", unsafe_allow_html=True)
    csv_bytes = display_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Export CSV",
        data=csv_bytes,
        file_name=f"newtrap_trades_{from_date}_{to_date}.csv",
        mime="text/csv",
    )
