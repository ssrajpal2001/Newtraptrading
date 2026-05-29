# CLAUDE.md — NewTrap Institutional Trading Platform
## Complete Context Document for AI Handoff

> This file is the authoritative source of truth for any AI agent continuing development on this codebase. Read every section before writing a single line of code.

---

## 1. What This System Is

**NewTrap** is a production-grade, option-chart-centric algorithmic trading platform for **NSE Nifty weekly options (Tuesday expiry)**. It is written entirely in Python.

### The Core Discipline — Read This First
The most important rule of this entire system:

> **NEVER analyse price action on the Nifty Spot Index chart or Futures chart. The spot index is fetched exactly once at 08:45 AM to compute a baseline. After that, ALL structural analysis — trend states, 75-min bar scanning, 5-min entries, stop-loss tracking — happens exclusively on the individual Option Contract Premium charts (CE and PE).**

If you are asked to add a feature, extend the engine, or fix a bug, always ask yourself: "Am I touching spot/futures data in any way other than the 08:45 AM baseline fetch?" If yes, stop and re-read the spec.

---

## 2. Repository Structure

```
Newtraptrading/
├── config.py                   # Session maths, ITM matrix, DayConfig
├── database.py                 # SQLAlchemy ORM + state helpers
├── data_feeder.py              # WebSocket feeders, bar aggregator, TrapDetector
├── execution_engine.py         # Multi-broker adapter + async order fan-out
├── oauth_handler.py            # Browser OAuth2 callback interceptor
├── app_ui.py                   # Streamlit root dashboard (Live Monitor)
├── main.py                     # Async orchestrator / entry point
├── pages/
│   ├── 1_Client_Management.py  # Client CRUD + OAuth connect per client
│   └── 2_Admin_Panel.py        # Feeder OAuth, morning init, trap controls
├── deploy/
│   ├── newtrap-engine.service  # systemd unit for main.py
│   ├── newtrap-ui.service      # systemd unit for Streamlit
│   ├── setup_ec2.sh            # Ubuntu 22.04 EC2 provisioning script
│   └── chrony.conf             # AWS NTP clock sync config
├── docs/
│   ├── ARCHITECTURE.md         # DB schema, state machines, dependency graph
│   ├── SETUP.md                # Broker API setup, token refresh
│   ├── USER_GUIDE.md           # Page-by-page UI walkthrough
│   └── AWS_DEPLOYMENT.md       # EC2 + SSH tunnel + systemd + Chrony
├── requirements.txt
├── .env.example
└── .gitignore
```

Total source lines: ~3,929 Python across 9 files.

---

## 3. Complete Feature Inventory (What Is Built)

### 3.1 config.py (178 lines)
**Status: Complete**

| Symbol | What it does |
|--------|-------------|
| `ITM_DISTANCE_MATRIX` | `{0:200, 1:100, 2:500, 3:400, 4:300}` — weekday→offset map |
| `compute_center_point(prev_open, prev_close)` | `(open + close) / 2` |
| `round_to_strike(value, step=50)` | Rounds to nearest 50-pt Nifty interval |
| `compute_day_config(date, prev_open, prev_close)` | Returns a `DayConfig` dataclass |
| `DayConfig` | `date, center_point, itm_offset, ce_strike, pe_strike, ce_symbol, pe_symbol` |
| `_next_tuesday_expiry_str(ref_date)` | Returns expiry string like `"03JUN25"` for symbol construction |
| `bar_key_for_minutes(ts, minutes)` | Truncates a datetime to the start of its N-minute bar |
| `is_market_open()` | True between 09:15–15:30 on weekdays |
| `is_morning_init_window()` | True from 08:45 AM on weekdays |
| `is_expiry_flush_time()` | True on Tuesday from 15:30 |
| `DATABASE_URL` | From env var, defaults to `sqlite:///newtrap_trading.db` |
| `HTF_BAR_MINUTES=75`, `LTF_BAR_MINUTES=5`, `RISK_BAR_MINUTES=1` | Timeframe constants |

**Strike formula:**
```
CE strike = round_to_50( CenterPoint - ITM_offset )   ← tracks bearish CE premium
PE strike = round_to_50( CenterPoint + ITM_offset )   ← tracks bullish PE premium
```

**Symbol format:** `NSE:NIFTY{DDMMMYY}{strike}{CE|PE}` e.g. `NSE:NIFTY03JUN2523500CE`

---

### 3.2 database.py (381 lines)
**Status: Complete**

**Engine:** SQLAlchemy 2.x, SQLite with WAL mode (or PostgreSQL via `DATABASE_URL`).

**Tables:**

`clients_registry`
- `id, name, broker(ENUM), api_key, api_secret, access_token, totp_secret, max_capital, active, created_at`
- `BrokerName` enum values: `ZERODHA, ANGEL_ONE, ALICE_BLUE, GROWW, UPSTOX, FYERS`

`historical_option_traps`
- `id, trade_date, strike, option_type(CE|PE), contract_symbol, entry_origin, target_high, candle_low_sl, status, voided_at, mitigated_at, notes`
- `TrapStatus` enum: `ACTIVE_UNMITIGATED → MITIGATED | VOIDED | EXPIRED_VOID`

`trades_ledger`
- `id, client_id(FK), trap_id(FK), contract_symbol, entry_price, exit_price, quantity, pnl, exit_category, entered_at, exited_at`
- `ExitCategory` enum: `TARGET_HIT, SL_HIT, MANUAL_SQUARE, EXPIRY_VOID`

**Key functions:**
```python
init_db()                                    # Create all tables
register_trap(strike, option_type, ...)      # → HistoricalOptionTraps (ACTIVE_UNMITIGATED)
set_trap_sl(trap_id, candle_low_sl)          # Attach 5-min SL level to trap
void_trap(trap_id)                           # ACTIVE → VOIDED
mitigate_trap(trap_id)                       # ACTIVE → MITIGATED
flush_expired_traps(contract_symbols=None)   # ACTIVE → EXPIRED_VOID (Tuesday flush)
get_active_traps(option_type=None)           # List[HistoricalOptionTraps]
get_next_lower_trap(current_entry, opt_type) # Cascade support: next trap below
record_trade_entry(client_id, symbol, ...)   # → TradesLedger
record_trade_exit(trade_id, exit_price, ...) # Updates pnl = (exit-entry)*qty
get_all_active_clients()                     # List[ClientsRegistry] where active=True
db_session()                                 # Context manager: commit/rollback/close
```

---

### 3.3 data_feeder.py (591 lines)
**Status: Core logic complete. See KNOWN GAPS for Upstox binary protocol.**

**Key classes:**

`OHLCV` — dataclass with `symbol, ts, open, high, low, close, volume, complete`. Method `.update(price, volume)` updates high/low/close.

`BarCache` — maintains a `Dict[(symbol, timeframe), OHLCV]`. On each tick, detects bar boundary transitions, seals the old bar (`complete=True`), calls `on_close()`, creates a new bar.

`TrapDetector` — stateful per-symbol processor. Receives closed bars from `BarAggregator`. Internal state machine:

```
[IDLE]
  → on bearish 75m candle  → [BEARISH_CANDLE_SET]  (store candle + high_sl)
  → on high SL breach      → register_trap() in DB → [HTF_TRAP_CONFIRMED]
  → premium in ±0.5% zone  → [IN_RETEST_ZONE]
  → bearish 5m candle      → [LTF_BEARISH_SET]
  → 5m high SL breach      → [LTF_TRAP_CONFIRMED]  (store entry_line + sl_line)
  → live tick ≤ entry_line → fire on_execute() → [IN_TRADE]
  → 1m close < sl_line     → void_trap() + on_sl_void() + cascade → [IDLE]
  → live price ≥ target    → mitigate_trap() → [IDLE]
```

`TrapDetector.__init__` params: `symbol, option_type, strike, on_execute, on_sl_void`
- `on_execute(symbol: str, price: float, trap_id: int)` — called at touch entry
- `on_sl_void(trap_id: int)` — called when 1-min close SL triggers

`BarAggregator` — consumes `TICK_QUEUE` (asyncio.Queue), builds 75m/5m/1m bars, routes closed bars to `TrapDetector.on_htf_bar_close()`, `.on_ltf_bar_close()`, `.on_risk_bar_close()`, and live ticks to `.on_tick()`.

`TICK_QUEUE: asyncio.Queue[Dict]` — shared queue. Tick format: `{"symbol": str, "price": float, "ts": str, "volume": float}`

`UpstoxFeeder` / `FyersFeeder` — async WebSocket classes with exponential-backoff reconnect (`[2,4,8,16,30]` seconds). Push parsed ticks into `TICK_QUEUE`.

`DualFeederSupervisor` — runs both feeders + a 50ms health monitor. If Upstox stale > 100ms, promotes Fyers. Both always push to the same queue; only the health flag is toggled.

---

### 3.4 execution_engine.py (487 lines)
**Status: Complete**

`BrokerAdapter` — abstract base with methods: `place_market_buy()`, `place_market_sell()`, `get_ltp()`, `get_margin()`. All return `Dict` or `float`.

Concrete adapters:
- `ZerodhaAdapter` — uses `kiteconnect` SDK
- `AngelOneAdapter` — uses `smartapi-python` + `pyotp`
- `AliceBlueAdapter` — uses `alice_blue` SDK
- `GrowwAdapter` — uses `aiohttp` REST calls

`build_adapter(client: ClientsRegistry)` — factory function, maps `BrokerName` → adapter class.

`ExecutionEngine` — module-level singleton `engine`.
```python
engine.reload_clients()              # Load active clients from DB, build adapters
engine.fire_entry(symbol, price, trap_id, quantity=1)   # Fan-out BUY to all clients
engine.fire_sl_exit(trap_id)         # Fan-out SELL (SL triggered)
engine.fire_target_exit(trap_id)     # Fan-out SELL (target hit)
engine.panic_square_off_all()        # Emergency close all positions
engine.get_all_margins()             # Dict[client_name, float]
engine.get_open_positions()          # Dict[client_id, (trade_id, symbol, entry_px)]
```

`_OPEN_POSITIONS: Dict[int, Tuple[int, str, float]]` — in-memory map of `client_id → (trade_id, symbol, entry_price)`. Cleared on exit.

---

### 3.5 oauth_handler.py (351 lines)
**Status: Complete**

`BROKER_OAUTH_CONFIG` — dict keyed by broker name string. Each entry has:
- `auth_url` — template with `{api_key}` and `{redirect_uri}` placeholders
- `token_url` — POST endpoint to exchange code for token
- `token_payload` — lambda `(code, api_key, api_secret, redirect_uri) → dict`
- `token_field` — key in the response JSON containing the access token

Brokers configured: `UPSTOX, FYERS, ZERODHA, ANGEL_ONE, ALICE_BLUE, GROWW`

`_OAuthCallbackHandler(BaseHTTPRequestHandler)` — captures `?code=` from GET request on `localhost:8080`. Sets `captured_code` class variable and fires `shutdown_flag` threading.Event.

`OAuthSession.run()` — full flow:
1. Start `HTTPServer` on `localhost:8080` in daemon thread
2. Build auth URL, call `webbrowser.open()`
3. `shutdown_flag.wait(timeout=300)` — blocks until code arrives or times out
4. Exchange code via `requests.post()` to `token_url`
5. Save token to `clients_registry.access_token` if `client_db_id` provided
6. Call `on_success(token)` callback
7. Shut down server

`start_oauth_flow_async(broker, api_key, api_secret, client_db_id, on_success)` — runs `OAuthSession.run()` in a daemon thread so Streamlit's render loop is not blocked.

**AWS / headless note:** On EC2, `webbrowser.open()` silently fails (no display). The Admin Panel and Client Management pages detect the pending state and should display the authorization URL as a clickable link for the user to copy into their local browser. The SSH tunnel (`ssh -L 8080:localhost:8080`) carries the callback back to EC2. **This clickable URL display in the UI is currently missing — it is a known gap.**

---

### 3.6 app_ui.py (596 lines)
**Status: Core dashboard built. Bar data wiring to charts is a known gap.**

Streamlit multi-page root. Renders at `http://localhost:8501`.

**Sections:**
- Obsidian Dark CSS (`#0D1117` canvas, `#161B22` cards, `#238636` CE/bull, `#DA3633` PE/bear, `#58A6FF` zones)
- Sidebar: date/time, market status, morning anchor metrics, active trap list, refresh button
- Top metrics row: CE/PE last price, active trap count, active clients, center point
- Orange retest flash banner (shown when `st.session_state.retest_active = True`)
- Two Plotly candlestick charts (CE and PE) with horizontal overlays for trap origin (blue dashed), entry (green), SL (red dashed), target (blue solid), retest zone (orange shaded)
- Historical trap registry table with `status` colour coding
- Client portfolio cards with live position indicator
- Global Master Panic Button (two-step: fire → confirm)

**Session state keys used:**
```python
st.session_state.day_config        # DayConfig | None
st.session_state.ce_bars           # List[tuple(ts, o, h, l, c)]
st.session_state.pe_bars           # List[tuple(ts, o, h, l, c)]
st.session_state.active_traps      # List[HistoricalOptionTraps]
st.session_state.clients           # List[ClientsRegistry]
st.session_state.retest_active     # bool
st.session_state.last_ce_price     # float | None
st.session_state.last_pe_price     # float | None
st.session_state.panic_confirmed   # bool
st.session_state.log_lines         # List[str]
```

`StreamlitDataBridge` — thread-safe buffer (`threading.Lock`) for injecting bar data from the async engine into Streamlit session state. Methods: `push_bar(symbol, bar_tuple)`, `flush_to_session()`. **The `push_bar()` method is never called by `BarAggregator` — wiring is a known gap.**

---

### 3.7 pages/1_Client_Management.py (503 lines)
**Status: Complete**

- Client table: all registered clients with stats (total trades, wins, net P&L)
- **Add New Client form**: name, broker dropdown, API key, secret, access token, TOTP, capital, active checkbox
- Per-client card actions: **Edit**, **Disable/Enable**, **View Trades**, **Delete** (with confirm), **🔓 Connect** (OAuth)
- `start_oauth_flow_async()` wired to the Connect button — opens broker login in browser, saves token to DB automatically
- OAuth status badge per card: `idle → pending → done → error`
- Inline edit form: pre-fills current values, token field intentionally blank (only overrides if non-empty)
- Trade history expansion: P&L-coloured table with exit category

---

### 3.8 pages/2_Admin_Panel.py (666 lines)
**Status: Complete**

**Section 1 — Data Feeder OAuth:**
- Two side-by-side forms (Upstox + Fyers) each with: API key, secret, optional client DB ID, Connect button
- Status badge: idle/pending/done/error with colour coding

**Section 2 — Morning Init:**
- Manual form: trade date, prev open, prev close → calls `compute_day_config()`
- Shows resulting DayConfig metrics and ITM matrix reference table

**Section 3 — Engine Health Metrics:**
- Queue depth from `TICK_QUEUE.qsize()`
- Open positions from `execution_engine._OPEN_POSITIONS`
- Today's session P&L from `trades_ledger` sum query

**Section 4 — Trap Registry:**
- Tab 1 "Add Trap Manually": full form → `register_trap()` + optional `set_trap_sl()`
- Tab 2 "Active Traps": styled dataframe of all `ACTIVE_UNMITIGATED` traps
- Tab 3 "Override Controls": dropdown select → Mitigate / Void / Update SL buttons

**Section 5 — Expiry Flush:** Button + confirmation → `flush_expired_traps()`

**Section 6 — Database Export:** Date-range filtered CSV download for both trades and traps using `st.download_button`

---

### 3.9 main.py (176 lines)
**Status: Skeleton complete. `_fetch_previous_day_ohlc()` is a stub — returns hardcoded values.**

Startup sequence:
1. `init_db()` + `engine.reload_clients()`
2. Waits for `is_morning_init_window()` — polls every 10s
3. Calls `morning_init()` → `compute_day_config()`
4. Creates `TrapDetector` for CE and PE, registers with `BarAggregator`
5. Creates `DualFeederSupervisor` and starts all tasks via `asyncio.gather`
6. `expiry_flush_watchdog` polls every 30s for Tuesday 15:30 flush
7. SIGINT/SIGTERM cancels all tasks gracefully

---

## 4. Known Gaps — What Needs to Be Built

These are the items explicitly not yet implemented. Work on these in the order listed for a production-ready system.

### ~~GAP 1: `_fetch_previous_day_ohlc()` — Real Historical Data Fetch~~ ✅ RESOLVED
**File:** `main.py`
**Resolution:** Replaced stub with `_fetch_upstox_previous_ohlc()` and `_fetch_fyers_previous_ohlc()`.
- Primary: Upstox V3 `GET https://api.upstox.com/v3/historical-candle/NSE_INDEX%7CNifty%2050/1day/{to}/{from}`
- Extracts `candles[0][1]` (open) and `candles[0][4]` (close) — descending order, index 0 = most recent day
- `_last_trading_day()` helper skips weekends to find the correct prior session
- Fyers fallback: `GET https://api.fyers.in/api/v2/history?symbol=NSE:NIFTY50-INDEX&resolution=D&...`
- Fyers candles are ascending; extracts `candles[-1][1]` and `candles[-1][4]`
- Raises `RuntimeError` if both fail so the operator is alerted instead of silently using stale data

### ~~GAP 2: Upstox Binary WebSocket Protocol~~ ✅ RESOLVED
**Files:** `data_feeder.py`, `proto/MarketDataFeed.proto`, `build_protos.py`
**Resolution:**
- `proto/MarketDataFeed.proto` — official Upstox proto definition (all message types: FeedResponse, Feed, FullFeed, MarketFullFeed, IndexFullFeed, LTPC, Level, MarketOHLC, OptionGreeks, compact/extended variants)
- `build_protos.py` — runs `grpc_tools.protoc` to compile → `proto/MarketDataFeed_pb2.py`
- `data_feeder.py` — imports `from proto import MarketDataFeed_pb2 as _pb2` with `try/except` graceful fallback
- `UpstoxFeeder` upgraded: URL → V3 `wss://api.upstox.com/v3/feed/market-data-feed`, `max_size=8MB`
- `_to_upstox_key()` converts `NSE:NIFTY03JUN2523500CE` → `NSE_FO|NIFTY2562323500CE` for subscription
- `_decode_upstox_binary(frame, key_to_display)` — full decode path:
  ```
  FeedResponse.ParseFromString(frame)
    .feeds[instrument_key]
      .fullFeed.marketFF.ltpc.ltp   ← option premium last traded price
      .fullFeed.marketFF.ltpc.ltt   ← epoch-ms timestamp → ISO string
      .fullFeed.marketFF.ltpc.ltq   ← last traded quantity → volume
  ```
- `deploy/setup_ec2.sh` updated to run `build_protos.py` during provisioning
- **Run `python build_protos.py` once after `pip install -r requirements.txt`**

### GAP 3: StreamlitDataBridge Not Wired to BarAggregator
**Files:** `app_ui.py:StreamlitDataBridge`, `data_feeder.py:BarAggregator`
**Current state:** `StreamlitDataBridge._data_bridge` exists and `flush_to_session()` is called in `app_ui.py:main()`, but `push_bar()` is never called from `BarAggregator`.
**What to build:** In `data_feeder.py:BarAggregator.run()`, after calling each `on_close` callback, also push the sealed bar to the bridge:
```python
from app_ui import _data_bridge   # import the module-level bridge instance
# inside on_close for HTF bars:
_data_bridge.push_bar(symbol, (bar.ts, bar.open, bar.high, bar.low, bar.close))
```
Avoid a circular import by moving `_data_bridge` to a standalone `bridge.py` module that both `data_feeder.py` and `app_ui.py` import from.

### GAP 4: OAuth Auth URL Not Displayed on Headless EC2
**Files:** `pages/2_Admin_Panel.py`, `pages/1_Client_Management.py`
**Current state:** After clicking Connect, only a generic info message appears. On EC2 (no display), `webbrowser.open()` silently does nothing.
**What to build:** In `oauth_handler.py:OAuthSession.run()`, after calling `webbrowser.open()`, also return the auth URL string. In the Admin Panel and Client Management pages, display this URL as a `st.code()` block and a `st.link_button()` so the user can copy/click it manually from their laptop browser.

### GAP 5: ATM Contract Selection at Entry
**Files:** `execution_engine.py:fire_entry()`, `data_feeder.py:TrapDetector.on_tick()`
**Current state:** `on_execute(symbol, price, trap_id)` passes `self.symbol` which is the **tracked ITM contract** (e.g., 500 points ITM on Wednesday). The spec requires the order to be placed on the **ATM contract** (closest strike to the current spot price at entry moment).
**What to build:** At the time of entry touch, fetch the current Nifty spot price (from the live tick stream or a REST call), compute `ATM_strike = round_to_50(spot_price)`, construct the ATM symbol, and pass that to `fire_entry()` instead of the tracked ITM symbol.

### GAP 6: Position Sizing by `max_capital`
**Files:** `execution_engine.py:fire_entry()`
**Current state:** `quantity=1` hardcoded for all clients.
**What to build:** For each client, compute quantity as `floor(client.max_capital / (entry_price * LOT_SIZE))` where `LOT_SIZE = 25` (current Nifty lot size). Cap at `max_capital`. Add `LOT_SIZE = 25` constant to `config.py`.

### GAP 7: Daily Token Auto-Refresh Script
**Files:** Not created yet
**What to build:** A `scripts/refresh_tokens.py` that:
1. At 08:30 AM, iterates all active clients
2. For each client, calls the broker's token-refresh API (or re-runs the OAuth flow non-interactively using stored TOTP secrets where possible, e.g., Angel One)
3. Updates `access_token` in the database
4. Runs as a cron job: `30 8 * * 1-5 /opt/newtraptrading/.venv/bin/python scripts/refresh_tokens.py`

### GAP 8: `pages/3_Trade_History.py` — Dedicated Trade History Page
**Referenced in:** `docs/USER_GUIDE.md`
**What to build:** A dedicated Streamlit page with:
- Date range filter
- Per-client filter dropdown
- Full trade table with all columns
- Summary statistics: total trades, win rate, average P&L, largest win/loss
- A Plotly P&L equity curve chart (cumulative P&L over time)
- Export CSV button

### GAP 9: Live Unrealized P&L on Open Positions
**Files:** `app_ui.py`, `execution_engine.py`
**Current state:** Realized P&L is stored in `trades_ledger.pnl`. Open positions show the symbol but not the current unrealized P&L.
**What to build:** In the client cards on the main dashboard, for each open position call `adapter.get_ltp(symbol)` and display `unrealized_pnl = (ltp - entry_price) * quantity`. Use a background thread to refresh LTP every 5 seconds.

### GAP 10: `retest_active` Session State Never Set to True
**File:** `app_ui.py`
**Current state:** `st.session_state.retest_active` is initialized to `False` and never set to `True` anywhere. The orange flash banner never appears.
**What to build:** In `TrapDetector.on_ltf_bar_close()`, when the premium enters the retest zone, write this state to a shared thread-safe flag (similar to `StreamlitDataBridge`) that `app_ui.py` reads on each refresh.

### GAP 11: `last_ce_price` / `last_pe_price` Never Populated
**File:** `app_ui.py`
**Current state:** Top metric cards show `₹—` because `st.session_state.last_ce_price` and `.last_pe_price` are never updated.
**What to build:** Extend `StreamlitDataBridge` with a `push_tick(symbol, price)` method, and call it from `BarAggregator.run()` on every tick. The bridge stores latest prices which `app_ui.py` reads on each refresh.

---

## 5. Architecture Rules — Follow These When Adding Code

### Module boundaries
- `config.py` has zero imports from other project modules. All other modules may import from `config.py`.
- `database.py` imports only from `config.py`. No circular deps.
- `data_feeder.py` imports from `config.py` and `database.py` only.
- `execution_engine.py` imports from `database.py` only.
- `oauth_handler.py` imports from `database.py` only.
- `app_ui.py` and `pages/` may import from any module.
- `main.py` imports from all modules to wire them together.

### Async vs threading
- `main.py`, `data_feeder.py`, `execution_engine.py` are **asyncio**-based. Use `async/await`, `asyncio.gather`, `asyncio.create_task`.
- Streamlit runs in a **synchronous** context. Use `threading.Thread` for background work. Use `asyncio.run_coroutine_threadsafe()` to call async code from Streamlit.
- `StreamlitDataBridge` uses `threading.Lock` to cross the thread boundary safely.
- `OAuthSession` uses `threading.Thread` + `threading.Event` because it involves blocking I/O (`webbrowser`, `requests.post`) that must not block Streamlit's render.

### Database session pattern
Always use the `db_session()` context manager:
```python
with db_session() as s:
    obj = s.query(Model).filter(...).first()
    s.expunge(obj)   # detach before returning — objects die when session closes
return obj
```
Never return ORM objects from inside a `with db_session()` block without calling `s.expunge()` first.

### Broker symbol format
```
NSE:NIFTY{DDMMMYY}{strike}{CE|PE}
e.g. NSE:NIFTY03JUN2523500CE
```
The `DayConfig.__post_init__` builds these strings using `_next_tuesday_expiry_str()`.

### Adding a new broker adapter
1. Add the broker to `BrokerName` enum in `database.py`
2. Add OAuth config to `BROKER_OAUTH_CONFIG` in `oauth_handler.py`
3. Create a new `XxxAdapter(BrokerAdapter)` class in `execution_engine.py`
4. Register it in the `_ADAPTER_MAP` dict in `execution_engine.py`

### Colour palette — always use these
| Usage | Hex |
|-------|-----|
| Canvas background | `#0D1117` |
| Card / component background | `#161B22` |
| CE / Bullish / Green | `#238636` |
| PE / Bearish / Red | `#DA3633` |
| Active zone / Blue | `#58A6FF` |
| Muted text | `#8B949E` |
| Borders | `#30363D` |
| Alert / Retest orange | `#f0a500` |

---

## 6. Running the System

### First-time setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
python -c "from database import init_db; init_db()"
```

### Development
```bash
# Terminal 1: trading engine
python main.py

# Terminal 2: dashboard
streamlit run app_ui.py
```

### AWS EC2 production
```bash
# First time: provision server
./deploy/setup_ec2.sh

# Daily operation
sudo systemctl start newtrap-engine newtrap-ui

# Logs
sudo journalctl -u newtrap-engine -f
sudo journalctl -u newtrap-ui -f
```

### SSH tunnel for OAuth (run on your local laptop)
```bash
ssh -L 8080:localhost:8080 -N -i your-key.pem ubuntu@EC2_IP
```

---

## 7. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `UPSTOX_API_KEY` | Yes (data) | Upstox app API key |
| `UPSTOX_API_SECRET` | Yes (data) | Upstox app secret |
| `UPSTOX_ACCESS_TOKEN` | Yes (data) | Daily bearer token |
| `FYERS_APP_ID` | Yes (fallback) | Fyers app ID |
| `FYERS_SECRET_KEY` | Yes (fallback) | Fyers secret |
| `FYERS_ACCESS_TOKEN` | Yes (fallback) | Daily bearer token |
| `DATABASE_URL` | No | Defaults to `sqlite:///newtrap_trading.db` |

Client broker credentials (Zerodha, Angel One, etc.) are stored in the `clients_registry` DB table, managed via the Client Management UI page.

---

## 8. Dependencies

```
websockets>=12.0        # WebSocket feeders
sqlalchemy>=2.0.0       # ORM
aiohttp>=3.9.0          # Groww REST adapter
streamlit>=1.35.0       # Dashboard
plotly>=5.22.0          # Candlestick charts
pandas>=2.2.0           # DataFrames
pyotp>=2.9.0            # Angel One TOTP
requests>=2.31.0        # OAuth token exchange
python-dotenv>=1.0.0    # .env loading

# Optional broker SDKs:
# kiteconnect>=4.2.0
# smartapi-python>=1.3.5
# alice_blue>=2.3.0
```

---

## 9. Testing Approach

There are no automated tests yet. When adding tests:
- Unit test `config.py` functions — pure maths, no I/O
- Unit test `database.py` state transitions using an in-memory SQLite DB (`DATABASE_URL = "sqlite:///:memory:"`)
- Unit test `TrapDetector` by feeding synthetic `OHLCV` objects and asserting `register_trap` was called
- Integration test the OAuth flow by mocking `webbrowser.open` and `requests.post`
- Do NOT unit test broker adapter network calls — mock the SDK objects instead

---

## 10. Key Design Decisions and Why

| Decision | Reason |
|----------|--------|
| Option-chart-only analysis | The strategy thesis is that institutional sellers write options and get trapped. The premium chart is where the structural evidence is, not the spot chart. |
| 75-min bars (not 1h) | 75 minutes = 5 Nifty 15-min bars = one major institutional accumulation cycle. The 75-min high SL breach is the confirmation signal. |
| asyncio for engine, threads for Streamlit | Streamlit is inherently synchronous. The engine needs concurrent I/O (two WebSocket feeds + DB writes). They share state through thread-safe bridges. |
| SQLite with WAL mode | WAL (Write-Ahead Logging) allows concurrent readers while the engine writes. Zero infrastructure — no Postgres server needed for single-machine deployment. |
| Touch entry (no candle confirmation) | The spec requires execution the microsecond the premium hits the 5-min sellers' entry line. Waiting for a close would introduce multiple seconds of slippage in fast-moving options. |
| 1-min candle-CLOSE SL | Protects against intraday wicks and temporary stop-hunts. Only a confirmed close below the structural low invalidates the setup. |
| Cascade to next lower trap | Avoids dead capital after a SL. The system immediately redirects attention to the next registered institutional level rather than sitting idle. |
| Dual broker with <100ms failover | NSE market hours are unforgiving — a 1-minute data gap during a 5-min bar formation can corrupt the entire bar and produce phantom signals. |
