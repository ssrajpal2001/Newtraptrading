"""
pages/2_Admin_Panel.py
System administration panel.

Sections
--------
  1. Data Feeder OAuth Connect  — Upstox & Fyers browser login
  2. Morning Initialisation     — manual trigger / override
  3. Engine Health Metrics      — feed status, queue depth, open positions
  4. Manual Trap Registry       — add / override / void traps by hand
  5. Expiry Flush               — Tuesday end-of-week cleanup button
  6. Database Export            — CSV download for trades & traps
"""

from __future__ import annotations

import io
import logging
import threading
from datetime import date, datetime

import pandas as pd
import streamlit as st

from config import (
    DayConfig,
    ITM_DISTANCE_MATRIX,
    compute_day_config,
    is_market_open,
)
from database import (
    HistoricalOptionTraps,
    OptionType,
    TrapStatus,
    ClientsRegistry,
    TradesLedger,
    db_session,
    flush_expired_traps,
    get_active_traps,
    init_db,
    mitigate_trap,
    register_trap,
    set_trap_sl,
    void_trap,
)
from oauth_handler import BROKER_OAUTH_CONFIG, start_oauth_flow_async
from database import BrokerName

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config & theme
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Admin Panel — NewTrap",
    page_icon="⚙️",
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
  .stNumberInput > div > div > input,
  .stDateInput > div > div > input {
    background: #161B22 !important;
    color: #C9D1D9 !important;
    border: 1px solid #30363D !important;
  }
  .stForm { background: #161B22; border: 1px solid #30363D;
            border-radius: 8px; padding: 16px; }
  .health-ok  { color: #238636; font-weight: bold; }
  .health-warn { color: #f0a500; font-weight: bold; }
  .health-err  { color: #DA3633; font-weight: bold; }
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)
init_db()

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

_STATE_DEFAULTS = {
    "upstox_token":        "",
    "fyers_token":         "",
    "upstox_oauth_status": "idle",       # idle | pending | done | error
    "fyers_oauth_status":  "idle",
    "day_config":          None,
    "flush_confirm":       False,
}
for k, v in _STATE_DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.markdown(
    "<h1 style='color:#58A6FF; letter-spacing:2px; font-size:1.5rem;'>"
    "⚙️  ADMIN PANEL</h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='color:#8B949E;'>System controls, data feeder authentication, "
    "morning initialisation, trap registry management, and database export.</p>",
    unsafe_allow_html=True,
)
st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 1: Data Feeder OAuth (Upstox + Fyers)
# ---------------------------------------------------------------------------

st.markdown("## 🔌 Data Feeder Authentication")
st.markdown(
    "<p style='color:#8B949E;'>Connect Upstox and Fyers market data feeders "
    "using the interactive browser OAuth flow.  "
    "Click <b>Connect</b> to open the broker login page in your browser.</p>",
    unsafe_allow_html=True,
)

feed_col1, feed_col2 = st.columns(2)

# ---- Upstox ----
with feed_col1:
    st.markdown(
        "<div style='background:#161B22; border:1px solid #30363D; "
        "border-radius:8px; padding:16px;'>",
        unsafe_allow_html=True,
    )
    st.markdown("### Upstox (Primary Feed)")

    status_map = {
        "idle":    ("⚫ Not connected", "#8B949E"),
        "pending": ("🟡 Waiting for browser login…", "#f0a500"),
        "done":    ("🟢 Connected", "#238636"),
        "error":   ("🔴 Error — retry", "#DA3633"),
    }
    s_label, s_color = status_map[st.session_state.upstox_oauth_status]
    st.markdown(
        f"**Status:** <span style='color:{s_color};'>{s_label}</span>",
        unsafe_allow_html=True,
    )
    if st.session_state.upstox_token:
        masked = st.session_state.upstox_token[:12] + "…"
        st.markdown(f"**Token:** `{masked}`")

    with st.form("upstox_oauth_form"):
        ux_api_key    = st.text_input("Upstox API Key *", placeholder="your_api_key")
        ux_api_secret = st.text_input("Upstox API Secret *", type="password",
                                       placeholder="your_api_secret")
        ux_client_id  = st.number_input(
            "Client DB ID (optional — leave 0 to skip DB save)",
            min_value=0, value=0, step=1,
        )
        ux_submit = st.form_submit_button("🔓 Connect Upstox", use_container_width=True)

    if ux_submit:
        if not ux_api_key or not ux_api_secret:
            st.error("API Key and Secret are required.")
        else:
            st.session_state.upstox_oauth_status = "pending"

            def _upstox_success(token: str) -> None:
                st.session_state.upstox_token        = token
                st.session_state.upstox_oauth_status = "done"

            _thread, auth_url = start_oauth_flow_async(
                broker="UPSTOX",
                api_key=ux_api_key.strip(),
                api_secret=ux_api_secret.strip(),
                client_db_id=int(ux_client_id) if ux_client_id > 0 else None,
                on_success=_upstox_success,
            )
            st.session_state["upstox_auth_url"] = auth_url
            st.info(
                "Browser opening for Upstox login.  "
                "Complete the login and return here — the token will update automatically."
            )

    if st.session_state.get("upstox_auth_url"):
        st.markdown("**If the browser did not open automatically (headless server), copy this URL to your local browser:**")
        st.code(st.session_state["upstox_auth_url"], language=None)
        st.link_button("Open Upstox Login", st.session_state["upstox_auth_url"])

    st.markdown("</div>", unsafe_allow_html=True)

# ---- Fyers ----
with feed_col2:
    st.markdown(
        "<div style='background:#161B22; border:1px solid #30363D; "
        "border-radius:8px; padding:16px;'>",
        unsafe_allow_html=True,
    )
    st.markdown("### Fyers (Fallback Feed)")

    s_label, s_color = status_map[st.session_state.fyers_oauth_status]
    st.markdown(
        f"**Status:** <span style='color:{s_color};'>{s_label}</span>",
        unsafe_allow_html=True,
    )
    if st.session_state.fyers_token:
        masked = st.session_state.fyers_token[:12] + "…"
        st.markdown(f"**Token:** `{masked}`")

    with st.form("fyers_oauth_form"):
        fy_app_id     = st.text_input("Fyers App ID *", placeholder="your_app_id")
        fy_secret_key = st.text_input("Fyers Secret Key *", type="password",
                                       placeholder="your_secret_key")
        fy_client_id  = st.number_input(
            "Client DB ID (optional)",
            min_value=0, value=0, step=1, key="fyers_client_id",
        )
        fy_submit = st.form_submit_button("🔓 Connect Fyers", use_container_width=True)

    if fy_submit:
        if not fy_app_id or not fy_secret_key:
            st.error("App ID and Secret Key are required.")
        else:
            st.session_state.fyers_oauth_status = "pending"

            def _fyers_success(token: str) -> None:
                st.session_state.fyers_token        = token
                st.session_state.fyers_oauth_status = "done"

            _thread, auth_url = start_oauth_flow_async(
                broker="FYERS",
                api_key=fy_app_id.strip(),
                api_secret=fy_secret_key.strip(),
                client_db_id=int(fy_client_id) if fy_client_id > 0 else None,
                on_success=_fyers_success,
            )
            st.session_state["fyers_auth_url"] = auth_url
            st.info(
                "Browser opening for Fyers login.  "
                "Complete the login and return here."
            )

    if st.session_state.get("fyers_auth_url"):
        st.markdown("**If the browser did not open automatically (headless server), copy this URL to your local browser:**")
        st.code(st.session_state["fyers_auth_url"], language=None)
        st.link_button("Open Fyers Login", st.session_state["fyers_auth_url"])

    st.markdown("</div>", unsafe_allow_html=True)

st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 2: Morning Initialisation
# ---------------------------------------------------------------------------

st.markdown("## 🌅 Morning Initialisation")
st.markdown(
    "<p style='color:#8B949E;'>"
    "Normally triggered automatically at 08:45 AM by the engine.  "
    "Use this form to override or re-run with custom OHLC values.</p>",
    unsafe_allow_html=True,
)

with st.form("morning_init_form"):
    mi_col1, mi_col2, mi_col3 = st.columns(3)
    with mi_col1:
        mi_date      = st.date_input("Trade Date", value=date.today())
    with mi_col2:
        mi_prev_open = st.number_input(
            "Previous Day Nifty Spot OPEN",
            min_value=1000.0, max_value=99999.0, value=23400.0, step=50.0,
        )
    with mi_col3:
        mi_prev_close = st.number_input(
            "Previous Day Nifty Spot CLOSE",
            min_value=1000.0, max_value=99999.0, value=23550.0, step=50.0,
        )

    mi_submit = st.form_submit_button("▶️  Run Morning Init", use_container_width=True)

if mi_submit:
    cfg = compute_day_config(mi_date, mi_prev_open, mi_prev_close)
    st.session_state.day_config = cfg
    st.success(
        f"Morning init complete — "
        f"Center Point: **{cfg.center_point:.2f}** | "
        f"ITM Offset: **±{cfg.itm_offset} pts** | "
        f"CE Strike: **{cfg.ce_strike}** | "
        f"PE Strike: **{cfg.pe_strike}**"
    )

if st.session_state.day_config:
    cfg = st.session_state.day_config
    dcol1, dcol2, dcol3, dcol4, dcol5 = st.columns(5)
    dcol1.metric("Center Point",  f"{cfg.center_point:.2f}")
    dcol2.metric("ITM Offset",    f"±{cfg.itm_offset}")
    dcol3.metric("CE Strike",     cfg.ce_strike)
    dcol4.metric("PE Strike",     cfg.pe_strike)
    dcol5.metric("Weekday Offset",
                 f"{cfg.trade_date.strftime('%A')} → {cfg.itm_offset}pts")

    st.markdown("**ITM Distance Matrix reference:**")
    matrix_df = pd.DataFrame([
        {"Weekday": "Monday",    "Offset": "±200 pts"},
        {"Weekday": "Tuesday",   "Offset": "±100 pts  (Expiry Day)"},
        {"Weekday": "Wednesday", "Offset": "±500 pts"},
        {"Weekday": "Thursday",  "Offset": "±400 pts"},
        {"Weekday": "Friday",    "Offset": "±300 pts"},
    ])
    st.dataframe(matrix_df, use_container_width=False, hide_index=True)

st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 3: Engine Health Metrics
# ---------------------------------------------------------------------------

st.markdown("## 📡 Engine Health Metrics")

try:
    from data_feeder import TICK_QUEUE
    queue_depth = TICK_QUEUE.qsize()
except Exception:
    queue_depth = "N/A (engine not running)"

try:
    from execution_engine import engine, _OPEN_POSITIONS
    open_pos_count = len(_OPEN_POSITIONS)
    engine_clients = len(engine._clients)
except Exception:
    open_pos_count = "N/A"
    engine_clients = "N/A"

active_traps = get_active_traps()

hcol1, hcol2, hcol3, hcol4, hcol5 = st.columns(5)
hcol1.metric("Market Status",    "OPEN" if is_market_open() else "CLOSED")
hcol2.metric("Active Traps",     len(active_traps))
hcol3.metric("Open Positions",   open_pos_count)
hcol4.metric("Engine Clients",   engine_clients)
hcol5.metric("Tick Queue Depth", queue_depth)

upstox_color = "#238636" if st.session_state.upstox_oauth_status == "done" else "#DA3633"
fyers_color  = "#238636" if st.session_state.fyers_oauth_status  == "done" else "#DA3633"

st.markdown(
    f"**Upstox Feed:** <span style='color:{upstox_color};'>"
    f"{'Connected' if st.session_state.upstox_oauth_status == 'done' else 'Not connected'}"
    f"</span> &nbsp;|&nbsp; "
    f"**Fyers Feed:** <span style='color:{fyers_color};'>"
    f"{'Connected' if st.session_state.fyers_oauth_status == 'done' else 'Not connected'}"
    f"</span>",
    unsafe_allow_html=True,
)

# Today's session P&L
try:
    with db_session() as s:
        from sqlalchemy import func as sqlfunc
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        pnl_today = s.query(sqlfunc.sum(TradesLedger.pnl)).filter(
            TradesLedger.entered_at >= today_start,
            TradesLedger.pnl.isnot(None),
        ).scalar() or 0.0
except Exception:
    pnl_today = 0.0

pnl_color = "#238636" if pnl_today >= 0 else "#DA3633"
st.markdown(
    f"**Today's Session P&L:** "
    f"<span style='color:{pnl_color}; font-size:1.2rem; font-weight:bold;'>"
    f"₹{pnl_today:,.2f}</span>",
    unsafe_allow_html=True,
)

st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 4: Manual Trap Registry
# ---------------------------------------------------------------------------

st.markdown("## 📋 Trap Registry Management")

tab_add, tab_active, tab_override = st.tabs([
    "➕ Add Trap Manually",
    "📊 Active Traps",
    "🔧 Override Controls",
])

# ---- Add Trap ----
with tab_add:
    st.markdown(
        "<p style='color:#8B949E;'>Use this to manually register an institutional "
        "level you have identified independently of the automated engine.</p>",
        unsafe_allow_html=True,
    )
    with st.form("manual_trap_form"):
        t1, t2, t3 = st.columns(3)
        with t1:
            tr_type    = st.selectbox("Option Type *", ["CE", "PE"])
            tr_strike  = st.number_input(
                "Nifty Strike *", min_value=10000, max_value=99999,
                value=23500, step=50,
            )
        with t2:
            tr_symbol  = st.text_input(
                "Contract Symbol *",
                placeholder="NSE:NIFTY15JUN2523500CE",
            )
            tr_origin  = st.number_input(
                "Entry Origin (premium) *",
                min_value=0.01, value=150.0, step=0.5,
            )
        with t3:
            tr_target  = st.number_input(
                "Target High (premium) *",
                min_value=0.01, value=200.0, step=0.5,
            )
            tr_sl      = st.number_input(
                "5-Min Candle Low (SL level)",
                min_value=0.0, value=0.0, step=0.5,
            )

        tr_submit = st.form_submit_button("💾 Register Trap", use_container_width=True)

    if tr_submit:
        if not tr_symbol or tr_origin >= tr_target:
            st.error("Symbol required; Target must be higher than Origin.")
        else:
            trap = register_trap(
                strike=int(tr_strike),
                option_type=OptionType(tr_type),
                contract_symbol=tr_symbol.strip(),
                entry_origin=tr_origin,
                target_high=tr_target,
            )
            if tr_sl > 0:
                set_trap_sl(trap.id, tr_sl)
            st.success(
                f"Trap registered — ID **{trap.id}** | "
                f"{tr_type} {tr_strike} | origin {tr_origin:.2f} → target {tr_target:.2f}"
            )
            st.rerun()

# ---- Active Traps ----
with tab_active:
    active = get_active_traps()
    if active:
        rows = []
        for t in active:
            rows.append({
                "ID":       t.id,
                "Date":     t.trade_date.strftime("%Y-%m-%d %H:%M") if t.trade_date else "",
                "Type":     t.option_type.value,
                "Strike":   t.strike,
                "Symbol":   t.contract_symbol,
                "Origin":   f"{t.entry_origin:.2f}",
                "Target":   f"{t.target_high:.2f}",
                "SL":       f"{t.candle_low_sl:.2f}" if t.candle_low_sl else "—",
            })
        df = pd.DataFrame(rows)

        def _type_color(val: str) -> str:
            return "color: #238636" if val == "CE" else "color: #DA3633"

        st.dataframe(
            df.style.applymap(_type_color, subset=["Type"]),
            use_container_width=True,
            hide_index=True,
        )
        st.markdown(f"_Total active traps: **{len(active)}**_")
    else:
        st.info("No active traps in the registry.")

# ---- Override Controls ----
with tab_override:
    active = get_active_traps()
    if not active:
        st.info("No active traps to override.")
    else:
        st.markdown("Select a trap ID to apply a manual override:")
        trap_ids = {f"ID {t.id} — {t.option_type.value} {t.strike} @ {t.entry_origin:.2f}": t.id
                    for t in active}
        selected_label = st.selectbox("Select Trap", list(trap_ids.keys()))
        selected_id    = trap_ids[selected_label]

        ov_col1, ov_col2, ov_col3 = st.columns(3)

        with ov_col1:
            if st.button("✅ Mark Mitigated (Target Hit)", key="ov_mitigate",
                         use_container_width=True):
                mitigate_trap(selected_id)
                st.success(f"Trap {selected_id} marked MITIGATED.")
                st.rerun()

        with ov_col2:
            if st.button("❌ Void Trap (SL Hit)", key="ov_void",
                         use_container_width=True):
                void_trap(selected_id)
                st.success(f"Trap {selected_id} VOIDED.")
                st.rerun()

        with ov_col3:
            with st.form(f"edit_sl_form_{selected_id}"):
                new_sl = st.number_input("Update SL Level", min_value=0.01, value=100.0)
                if st.form_submit_button("💾 Update SL"):
                    set_trap_sl(selected_id, new_sl)
                    st.success(f"SL updated to {new_sl:.2f} for trap {selected_id}.")
                    st.rerun()

st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 5: Expiry Flush
# ---------------------------------------------------------------------------

st.markdown("## 🗑️ Weekly Expiry Flush")
st.markdown(
    "<p style='color:#8B949E;'>"
    "Marks all <code>ACTIVE_UNMITIGATED</code> traps as <code>EXPIRED_VOID</code>. "
    "Normally runs automatically every Tuesday at 15:30 PM.  "
    "Use this button to trigger it manually (e.g., for early market closure days).</p>",
    unsafe_allow_html=True,
)

flush_col1, flush_col2, _ = st.columns([1, 1, 4])
with flush_col1:
    if st.button("🚿  Run Expiry Flush", use_container_width=True):
        st.session_state.flush_confirm = True

if st.session_state.flush_confirm:
    with flush_col2:
        st.warning("Confirm? This archives ALL open traps.")
        conf_c1, conf_c2 = st.columns(2)
        with conf_c1:
            if st.button("✅ Yes, Flush", key="flush_yes"):
                count = flush_expired_traps()
                st.session_state.flush_confirm = False
                st.success(f"{count} traps archived as EXPIRED_VOID.")
                st.rerun()
        with conf_c2:
            if st.button("❌ Cancel", key="flush_cancel"):
                st.session_state.flush_confirm = False
                st.rerun()

st.markdown("---")


# ---------------------------------------------------------------------------
# SECTION 6: Database Export
# ---------------------------------------------------------------------------

st.markdown("## 📥 Database Export")

exp_col1, exp_col2 = st.columns(2)

with exp_col1:
    st.markdown("### Trades Ledger")
    exp_date_from = st.date_input("From", value=date.today(), key="exp_trade_from")
    exp_date_to   = st.date_input("To",   value=date.today(), key="exp_trade_to")

    if st.button("⬇️ Export Trades CSV", use_container_width=True):
        try:
            with db_session() as s:
                rows = (
                    s.query(TradesLedger)
                    .filter(
                        TradesLedger.entered_at >= datetime.combine(exp_date_from, datetime.min.time()),
                        TradesLedger.entered_at <= datetime.combine(exp_date_to,   datetime.max.time()),
                    )
                    .all()
                )
                data = [
                    {
                        "trade_id":       r.id,
                        "client_id":      r.client_id,
                        "trap_id":        r.trap_id,
                        "symbol":         r.contract_symbol,
                        "entry_price":    r.entry_price,
                        "exit_price":     r.exit_price,
                        "quantity":       r.quantity,
                        "pnl":            r.pnl,
                        "exit_category":  r.exit_category.value if r.exit_category else "",
                        "entered_at":     r.entered_at.isoformat() if r.entered_at else "",
                        "exited_at":      r.exited_at.isoformat()  if r.exited_at  else "",
                    }
                    for r in rows
                ]
                for r in rows:
                    s.expunge(r)

            csv_buf = io.StringIO()
            pd.DataFrame(data).to_csv(csv_buf, index=False)
            st.download_button(
                "📄 Download trades.csv",
                data=csv_buf.getvalue(),
                file_name=f"trades_{exp_date_from}_{exp_date_to}.csv",
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Export error: {exc}")

with exp_col2:
    st.markdown("### Trap Registry")

    if st.button("⬇️ Export Traps CSV", use_container_width=True):
        try:
            with db_session() as s:
                rows = s.query(HistoricalOptionTraps).all()
                data = [
                    {
                        "trap_id":         r.id,
                        "trade_date":      r.trade_date.isoformat() if r.trade_date else "",
                        "strike":          r.strike,
                        "option_type":     r.option_type.value,
                        "contract_symbol": r.contract_symbol,
                        "entry_origin":    r.entry_origin,
                        "target_high":     r.target_high,
                        "candle_low_sl":   r.candle_low_sl,
                        "status":          r.status.value,
                        "voided_at":       r.voided_at.isoformat()    if r.voided_at    else "",
                        "mitigated_at":    r.mitigated_at.isoformat()  if r.mitigated_at else "",
                        "notes":           r.notes,
                    }
                    for r in rows
                ]
                for r in rows:
                    s.expunge(r)

            csv_buf = io.StringIO()
            pd.DataFrame(data).to_csv(csv_buf, index=False)
            st.download_button(
                "📄 Download traps.csv",
                data=csv_buf.getvalue(),
                file_name=f"traps_all.csv",
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Export error: {exc}")

st.markdown("---")

# DB row count summary
st.markdown("### Database Statistics")
try:
    with db_session() as s:
        from sqlalchemy import func as sqlfunc
        n_clients = s.query(sqlfunc.count(ClientsRegistry.id)).scalar()
        n_traps   = s.query(sqlfunc.count(HistoricalOptionTraps.id)).scalar()
        n_trades  = s.query(sqlfunc.count(TradesLedger.id)).scalar()

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Total Clients",     n_clients)
    sc2.metric("Total Trap Records", n_traps)
    sc3.metric("Total Trade Records", n_trades)
except Exception as exc:
    st.warning(f"Could not query DB stats: {exc}")
