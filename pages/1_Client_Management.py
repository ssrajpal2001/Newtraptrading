"""
pages/1_Client_Management.py
Full CRUD interface for registered trading clients.

Features
--------
  • Add new client with broker credentials and capital limits
  • Browser OAuth "Connect" button per client (Zerodha, Angel One, Alice Blue, Groww)
  • Edit existing client (update tokens, capital, enable/disable)
  • Per-client trade history with P&L breakdown
  • Disable / re-enable without deleting records
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import func

from database import (
    BrokerName,
    ClientsRegistry,
    ExitCategory,
    TradesLedger,
    db_session,
    get_all_active_clients,
    init_db,
)
from oauth_handler import start_oauth_flow_async

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config & theme
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Client Management — NewTrap",
    page_icon="👥",
    layout="wide",
)

DARK_CSS = """
<style>
  html, body, [class*="css"] {
    background-color: #0D1117 !important;
    color: #C9D1D9 !important;
    font-family: 'JetBrains Mono', monospace !important;
  }
  .stApp { background-color: #0D1117; }
  section[data-testid="stSidebar"] { background-color: #161B22 !important; }
  div[data-testid="metric-container"] {
    background: #161B22; border: 1px solid #30363D;
    border-radius: 6px; padding: 12px;
  }
  .stButton > button {
    background: #161B22; color: #C9D1D9;
    border: 1px solid #30363D; border-radius: 4px;
  }
  .stButton > button:hover { border-color: #58A6FF; }
  .stTextInput > div > div > input,
  .stSelectbox > div > div,
  .stNumberInput > div > div > input {
    background: #161B22 !important;
    color: #C9D1D9 !important;
    border: 1px solid #30363D !important;
  }
  .stForm { background: #161B22; border: 1px solid #30363D;
            border-radius: 8px; padding: 16px; }
  .pnl-positive { color: #238636; font-weight: bold; }
  .pnl-negative { color: #DA3633; font-weight: bold; }
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)

init_db()


# ---------------------------------------------------------------------------
# DB helpers (client CRUD)
# ---------------------------------------------------------------------------

def _get_all_clients() -> list[ClientsRegistry]:
    with db_session() as s:
        clients = s.query(ClientsRegistry).order_by(ClientsRegistry.id).all()
        for c in clients:
            s.expunge(c)
        return clients


def _create_client(
    name: str,
    broker: str,
    api_key: str,
    api_secret: str,
    access_token: str,
    totp_secret: str,
    max_capital: float,
    active: bool,
) -> None:
    with db_session() as s:
        s.add(ClientsRegistry(
            name=name,
            broker=BrokerName(broker),
            api_key=api_key,
            api_secret=api_secret,
            access_token=access_token,
            totp_secret=totp_secret,
            max_capital=max_capital,
            active=active,
        ))


def _update_client(
    client_id: int,
    name: str,
    api_key: str,
    api_secret: str,
    access_token: str,
    totp_secret: str,
    max_capital: float,
    active: bool,
) -> None:
    with db_session() as s:
        c = s.get(ClientsRegistry, client_id)
        if c:
            c.name          = name
            c.api_key       = api_key
            c.api_secret    = api_secret
            c.access_token  = access_token
            c.totp_secret   = totp_secret
            c.max_capital   = max_capital
            c.active        = active


def _toggle_active(client_id: int, state: bool) -> None:
    with db_session() as s:
        c = s.get(ClientsRegistry, client_id)
        if c:
            c.active = state


def _delete_client(client_id: int) -> None:
    with db_session() as s:
        c = s.get(ClientsRegistry, client_id)
        if c:
            s.delete(c)


def _get_trades_for_client(client_id: int) -> list[dict]:
    with db_session() as s:
        trades = (
            s.query(TradesLedger)
            .filter(TradesLedger.client_id == client_id)
            .order_by(TradesLedger.entered_at.desc())
            .limit(100)
            .all()
        )
        rows = []
        for t in trades:
            rows.append({
                "Trade ID":      t.id,
                "Symbol":        t.contract_symbol,
                "Entry ₹":       f"{t.entry_price:.2f}",
                "Exit ₹":        f"{t.exit_price:.2f}" if t.exit_price else "OPEN",
                "Qty":           t.quantity,
                "P&L ₹":         t.pnl,
                "Exit Type":     t.exit_category.value if t.exit_category else "—",
                "Entered At":    t.entered_at.strftime("%Y-%m-%d %H:%M:%S") if t.entered_at else "",
                "Exited At":     t.exited_at.strftime("%Y-%m-%d %H:%M:%S") if t.exited_at else "—",
            })
        return rows


def _client_summary_stats(client_id: int) -> dict:
    with db_session() as s:
        total = s.query(func.count(TradesLedger.id)).filter(
            TradesLedger.client_id == client_id
        ).scalar() or 0
        pnl_sum = s.query(func.sum(TradesLedger.pnl)).filter(
            TradesLedger.client_id == client_id,
            TradesLedger.pnl.isnot(None),
        ).scalar() or 0.0
        wins = s.query(func.count(TradesLedger.id)).filter(
            TradesLedger.client_id == client_id,
            TradesLedger.pnl > 0,
        ).scalar() or 0
        return {"total": total, "pnl": pnl_sum, "wins": wins}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "edit_client_id" not in st.session_state:
    st.session_state.edit_client_id = None
if "expand_trades" not in st.session_state:
    st.session_state.expand_trades = set()
if "confirm_delete" not in st.session_state:
    st.session_state.confirm_delete = None


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.markdown(
    "<h1 style='color:#58A6FF; letter-spacing:2px; font-size:1.5rem;'>"
    "👥  CLIENT MANAGEMENT</h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='color:#8B949E;'>Register and manage trading client accounts, "
    "update daily access tokens, and review per-client P&amp;L history.</p>",
    unsafe_allow_html=True,
)
st.markdown("---")


# ---------------------------------------------------------------------------
# Add New Client form
# ---------------------------------------------------------------------------

with st.expander("➕  Add New Client", expanded=False):
    with st.form("add_client_form", clear_on_submit=True):
        st.markdown("#### New Client Details")
        c1, c2 = st.columns(2)
        with c1:
            new_name    = st.text_input("Display Name *", placeholder="e.g. Rajan — Zerodha")
            new_broker  = st.selectbox("Broker *", [b.value for b in BrokerName])
            new_capital = st.number_input("Max Capital (₹) *", min_value=1000.0,
                                          value=100_000.0, step=10_000.0)
            new_active  = st.checkbox("Active (include in order routing)", value=True)
        with c2:
            new_api_key    = st.text_input("API Key *", placeholder="Broker API key or username")
            new_api_secret = st.text_input("API Secret *", type="password",
                                           placeholder="Broker API secret or password")
            new_token      = st.text_input("Access Token *", type="password",
                                           placeholder="Daily bearer / session token")
            new_totp       = st.text_input("TOTP Secret", type="password",
                                           placeholder="Base32 seed (Angel One / 2FA)")

        submitted = st.form_submit_button("💾  Save Client", use_container_width=True)
        if submitted:
            if not new_name or not new_api_key or not new_api_secret:
                st.error("Name, API Key, and API Secret are required.")
            else:
                _create_client(
                    name=new_name.strip(),
                    broker=new_broker,
                    api_key=new_api_key.strip(),
                    api_secret=new_api_secret.strip(),
                    access_token=new_token.strip(),
                    totp_secret=new_totp.strip(),
                    max_capital=new_capital,
                    active=new_active,
                )
                st.success(
                    f"Client **{new_name}** added. "
                    f"Use the **🔓 Connect** button on their card to generate a live access token."
                )
                st.rerun()

st.markdown("---")


# ---------------------------------------------------------------------------
# Client list
# ---------------------------------------------------------------------------

clients = _get_all_clients()

if not clients:
    st.info("No clients registered yet. Use the form above to add your first client.")
    st.stop()

# Summary metrics
total_capital = sum(c.max_capital for c in clients if c.active)
active_count  = sum(1 for c in clients if c.active)

m1, m2, m3 = st.columns(3)
m1.metric("Total Clients", len(clients))
m2.metric("Active Clients", active_count)
m3.metric("Total Active Capital", f"₹{total_capital:,.0f}")

st.markdown("---")
st.markdown("### Registered Clients")


# ---------------------------------------------------------------------------
# Per-client card
# ---------------------------------------------------------------------------

for client in clients:
    status_color  = "#238636" if client.active else "#8B949E"
    status_label  = "🟢 ACTIVE" if client.active else "⚫ INACTIVE"
    broker_label  = client.broker.value if hasattr(client.broker, "value") else str(client.broker)

    with st.container():
        st.markdown(
            f"<div style='background:#161B22; border:1px solid #30363D; "
            f"border-radius:8px; padding:16px; margin-bottom:12px;'>",
            unsafe_allow_html=True,
        )

        # Header row
        head_col1, head_col2, head_col3 = st.columns([4, 2, 4])
        with head_col1:
            st.markdown(
                f"<span style='color:#58A6FF; font-size:1.05rem; font-weight:bold;'>"
                f"{client.name}</span>  "
                f"<span style='color:#8B949E; font-size:0.85rem;'>#{client.id} · {broker_label}</span>",
                unsafe_allow_html=True,
            )
        with head_col2:
            st.markdown(
                f"<span style='color:{status_color}; font-weight:bold;'>{status_label}</span>",
                unsafe_allow_html=True,
            )
        with head_col3:
            stats = _client_summary_stats(client.id)
            pnl_color = "#238636" if stats["pnl"] >= 0 else "#DA3633"
            st.markdown(
                f"<span style='color:#8B949E;'>Trades: </span><b>{stats['total']}</b> &nbsp;"
                f"<span style='color:#8B949E;'>Wins: </span><b>{stats['wins']}</b> &nbsp;"
                f"<span style='color:#8B949E;'>Net P&L: </span>"
                f"<b style='color:{pnl_color};'>₹{stats['pnl']:,.2f}</b>",
                unsafe_allow_html=True,
            )

        # Info row
        info_col1, info_col2, info_col3, info_col4 = st.columns(4)
        info_col1.markdown(f"**Capital:** ₹{client.max_capital:,.0f}")
        info_col2.markdown(f"**Created:** {client.created_at.strftime('%Y-%m-%d') if client.created_at else '—'}")
        token_masked = (client.access_token[:8] + "…") if client.access_token else "not set"
        info_col3.markdown(f"**Token:** `{token_masked}`")
        info_col4.markdown(f"**TOTP:** {'configured' if client.totp_secret else 'not set'}")

        # Action buttons
        btn_col1, btn_col2, btn_col3, btn_col4, btn_col5, _ = st.columns([1, 1, 1, 1, 1, 2])

        with btn_col1:
            if st.button("✏️ Edit", key=f"edit_{client.id}"):
                st.session_state.edit_client_id = (
                    client.id if st.session_state.edit_client_id != client.id else None
                )

        with btn_col2:
            if client.active:
                if st.button("⏸ Disable", key=f"disable_{client.id}"):
                    _toggle_active(client.id, False)
                    st.rerun()
            else:
                if st.button("▶️ Enable", key=f"enable_{client.id}"):
                    _toggle_active(client.id, True)
                    st.rerun()

        with btn_col3:
            trades_open = client.id in st.session_state.expand_trades
            label = "📊 Hide Trades" if trades_open else "📊 View Trades"
            if st.button(label, key=f"trades_{client.id}"):
                if trades_open:
                    st.session_state.expand_trades.discard(client.id)
                else:
                    st.session_state.expand_trades.add(client.id)
                st.rerun()

        with btn_col4:
            if st.button("🗑️ Delete", key=f"delete_{client.id}"):
                st.session_state.confirm_delete = client.id

        with btn_col5:
            # Browser OAuth token refresh — opens broker login in system browser
            oauth_key = f"oauth_status_{client.id}"
            if oauth_key not in st.session_state:
                st.session_state[oauth_key] = "idle"

            btn_label = {
                "idle":    "🔓 Connect",
                "pending": "⏳ Waiting…",
                "done":    "✅ Connected",
                "error":   "⚠️ Retry",
            }.get(st.session_state[oauth_key], "🔓 Connect")

            if st.button(btn_label, key=f"oauth_{client.id}"):
                broker_val = (
                    client.broker.value
                    if hasattr(client.broker, "value")
                    else str(client.broker)
                )
                st.session_state[oauth_key] = "pending"

                def _make_success_cb(cid: int, skey: str):
                    def _cb(token: str) -> None:
                        st.session_state[skey] = "done"
                    return _cb

                _thread, auth_url = start_oauth_flow_async(
                    broker=broker_val,
                    api_key=client.api_key,
                    api_secret=client.api_secret,
                    client_db_id=client.id,
                    on_success=_make_success_cb(client.id, oauth_key),
                )
                st.session_state[f"auth_url_{client.id}"] = auth_url
                st.info(
                    f"Browser opening for **{client.name}** ({broker_val}) login. "
                    f"Complete the login — token updates automatically."
                )

        if st.session_state.get(f"auth_url_{client.id}"):
            auth_url_val = st.session_state[f"auth_url_{client.id}"]
            st.markdown(
                "**Headless server? Copy this URL to your local browser:**"
            )
            st.code(auth_url_val, language=None)
            st.link_button(f"Open {broker_val} Login", auth_url_val)

        # Confirm delete dialog
        if st.session_state.confirm_delete == client.id:
            st.warning(
                f"⚠️ Delete **{client.name}**? This will also delete all their trade records."
            )
            del_c1, del_c2, _ = st.columns([1, 1, 5])
            with del_c1:
                if st.button("✅ Confirm Delete", key=f"confirm_del_{client.id}"):
                    _delete_client(client.id)
                    st.session_state.confirm_delete = None
                    st.success(f"Client {client.name} deleted.")
                    st.rerun()
            with del_c2:
                if st.button("❌ Cancel", key=f"cancel_del_{client.id}"):
                    st.session_state.confirm_delete = None
                    st.rerun()

        # Edit form (inline, shown when edit button clicked)
        if st.session_state.edit_client_id == client.id:
            st.markdown("---")
            st.markdown("**Edit Client**")
            with st.form(f"edit_form_{client.id}"):
                ec1, ec2 = st.columns(2)
                with ec1:
                    e_name    = st.text_input("Display Name", value=client.name)
                    e_capital = st.number_input(
                        "Max Capital (₹)", min_value=1000.0,
                        value=float(client.max_capital), step=10_000.0
                    )
                    e_active  = st.checkbox("Active", value=bool(client.active))
                with ec2:
                    e_api_key    = st.text_input("API Key", value=client.api_key)
                    e_api_secret = st.text_input("API Secret", type="password",
                                                  value=client.api_secret)
                    e_token      = st.text_input(
                        "Access Token (paste new daily token)",
                        type="password",
                        value="",
                        placeholder="Leave blank to keep existing token",
                    )
                    e_totp = st.text_input(
                        "TOTP Secret",
                        type="password",
                        value=client.totp_secret or "",
                    )

                save_edit = st.form_submit_button("💾 Update Client", use_container_width=True)
                if save_edit:
                    _update_client(
                        client_id=client.id,
                        name=e_name.strip() or client.name,
                        api_key=e_api_key.strip() or client.api_key,
                        api_secret=e_api_secret.strip() or client.api_secret,
                        access_token=e_token.strip() if e_token.strip() else client.access_token,
                        totp_secret=e_totp.strip(),
                        max_capital=e_capital,
                        active=e_active,
                    )
                    st.session_state.edit_client_id = None
                    st.success("Client updated.")
                    st.rerun()

        # Trade history table
        if client.id in st.session_state.expand_trades:
            st.markdown("---")
            st.markdown(f"**Trade History — {client.name}**")
            trade_rows = _get_trades_for_client(client.id)
            if trade_rows:
                df = pd.DataFrame(trade_rows)

                def _color_pnl(val):
                    if val is None:
                        return ""
                    return "color: #238636" if float(val) >= 0 else "color: #DA3633"

                styled = df.style.applymap(_color_pnl, subset=["P&L ₹"])
                st.dataframe(styled, use_container_width=True, hide_index=True)

                total_pnl = sum(
                    r["P&L ₹"] for r in trade_rows if r["P&L ₹"] is not None
                )
                pnl_color = "#238636" if total_pnl >= 0 else "#DA3633"
                st.markdown(
                    f"<b>Total P&L across {len(trade_rows)} trades: "
                    f"<span style='color:{pnl_color};'>₹{total_pnl:,.2f}</span></b>",
                    unsafe_allow_html=True,
                )
            else:
                st.info("No trades recorded for this client yet.")

        st.markdown("</div>", unsafe_allow_html=True)
