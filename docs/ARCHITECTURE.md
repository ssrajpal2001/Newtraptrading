# System Architecture — NewTrap Trading Platform

---

## 1. Module Dependency Graph

```
main.py
  ├── config.py          (compute_day_config, DayConfig, bar_key_for_minutes)
  ├── database.py        (init_db, flush_expired_traps, get_active_traps)
  ├── data_feeder.py
  │     ├── config.py    (HTF_BAR_MINUTES, LTF_BAR_MINUTES, RISK_BAR_MINUTES)
  │     └── database.py  (register_trap, void_trap, mitigate_trap, set_trap_sl)
  └── execution_engine.py
        └── database.py  (get_all_active_clients, record_trade_entry, record_trade_exit)

app_ui.py
  ├── config.py
  ├── database.py
  └── execution_engine.py

pages/1_Client_Management.py
  └── database.py

pages/2_Admin_Panel.py
  ├── config.py
  ├── database.py
  └── execution_engine.py
```

---

## 2. Database Schema

### Table: `clients_registry`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `name` | VARCHAR(120) | Display name |
| `broker` | ENUM | ZERODHA / ANGEL_ONE / ALICE_BLUE / GROWW / UPSTOX / FYERS |
| `api_key` | VARCHAR(256) | Broker API key / username |
| `api_secret` | VARCHAR(256) | Broker API secret / password |
| `access_token` | VARCHAR(1024) | Daily bearer token (refreshed externally) |
| `totp_secret` | VARCHAR(128) | TOTP seed for 2FA (Angel One etc.) |
| `max_capital` | FLOAT | Maximum capital allocation in INR |
| `active` | BOOLEAN | Engine will only route orders to active clients |
| `created_at` | DATETIME | Record creation timestamp |

### Table: `historical_option_traps`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `trade_date` | DATETIME | Session date when trap was detected |
| `strike` | INTEGER | Nifty strike price |
| `option_type` | ENUM | CE or PE |
| `contract_symbol` | VARCHAR(64) | Full NSE symbol string |
| `entry_origin` | FLOAT | Premium level where sellers wrote their short (origin) |
| `target_high` | FLOAT | High of the 75-min trapped candle (exit target) |
| `candle_low_sl` | FLOAT | Low of the 5-min trap candle (stop-loss boundary) |
| `status` | ENUM | See trap lifecycle below |
| `voided_at` | DATETIME | Timestamp when trap was voided / expired |
| `mitigated_at` | DATETIME | Timestamp when target was reached |
| `notes` | VARCHAR(512) | Optional manual notes |

### Table: `trades_ledger`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `client_id` | INTEGER FK | → clients_registry.id |
| `trap_id` | INTEGER FK | → historical_option_traps.id |
| `contract_symbol` | VARCHAR(64) | Contract traded |
| `entry_price` | FLOAT | Execution entry premium |
| `exit_price` | FLOAT | Execution exit premium |
| `quantity` | INTEGER | Lot quantity |
| `pnl` | FLOAT | (exit − entry) × quantity |
| `exit_category` | ENUM | TARGET_HIT / SL_HIT / MANUAL_SQUARE / EXPIRY_VOID |
| `entered_at` | DATETIME | Order fire timestamp |
| `exited_at` | DATETIME | Exit timestamp |

---

## 3. Trap Lifecycle State Machine

```
                    ┌────────────────────────┐
                    │   [TRAP REGISTERED]    │
                    │  ACTIVE_UNMITIGATED    │
                    └───────────┬────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
     │  MITIGATED   │  │   VOIDED     │  │  EXPIRED_VOID    │
     │ Target hit   │  │ 1-min SL     │  │ Tuesday 15:30    │
     │ on premium   │  │ close below  │  │ weekly flush     │
     │ chart        │  │ trap candle  │  │                  │
     └──────────────┘  └──────────────┘  └──────────────────┘
```

---

## 4. TrapDetector State Machine (per symbol)

```
[IDLE]
   │
   │  75-min bar closes bearish (close < open)
   ▼
[BEARISH_CANDLE_SET]
   │  record: bearish_candle.high = HIGH_SL boundary
   │
   │  Next 75-min bar.high > HIGH_SL
   ▼
[HTF_TRAP_CONFIRMED]  ──────────────────────► DB: register_trap()
   │  entry_origin = bearish_candle.open
   │  target_high  = current_bar.high
   │
   │  Live premium ticks down toward entry_origin (±0.5% band)
   ▼
[IN_RETEST_ZONE]  ─────────────────────────► UI: flash orange banner
   │
   │  5-min bearish candle forms inside zone
   ▼
[LTF_BEARISH_SET]
   │
   │  5-min bar.high > 5-min bearish candle.high
   ▼
[LTF_TRAP_CONFIRMED]  ─────────────────────► DB: set_trap_sl()
   │  ltf_entry_line = 5min_bearish.open
   │  ltf_sl_line    = 5min_bearish.low
   │
   │  Live tick: price ≤ ltf_entry_line
   ▼
[IN_TRADE]  ───────────────────────────────► ExecutionEngine.fire_entry()
   │
   │
   ├── 1-min close < ltf_sl_line
   │       └──► void_trap() → cascade to next layer → ExecutionEngine.fire_sl_exit()
   │
   └── live price ≥ target_high
           └──► mitigate_trap() → ExecutionEngine.fire_target_exit()
```

---

## 5. Dual-Feeder Failover Architecture

```
  ┌─────────────────┐          ┌─────────────────┐
  │  Upstox WS      │          │  Fyers WS        │
  │  (PRIMARY)      │          │  (FALLBACK)      │
  └────────┬────────┘          └────────┬─────────┘
           │ ticks                      │ ticks
           │                            │
           └──────────┬─────────────────┘
                      │  both push to shared TICK_QUEUE
                      ▼
           ┌────────────────────┐
           │  DualFeederSupervisor │
           │                    │
           │  poll every 50ms:  │
           │  if primary stale  │
           │  > 100ms →         │
           │  promote fallback  │
           └────────┬───────────┘
                    │
                    ▼
           ┌────────────────────┐
           │   BarAggregator    │
           │  (consumes queue)  │
           └────────────────────┘
```

---

## 6. Execution Fan-Out Architecture

```
  Signal: touch_entry (symbol, price, trap_id)
                    │
                    ▼
         ExecutionEngine.fire_entry()
                    │
         asyncio.gather([
           ├── ZerodhaAdapter.place_market_buy()    ─► client_1 account
           ├── AngelOneAdapter.place_market_buy()   ─► client_2 account
           ├── AliceBlueAdapter.place_market_buy()  ─► client_3 account
           └── GrowwAdapter.place_market_buy()      ─► client_4 account
         ])
                    │
         record_trade_entry() ─► DB: trades_ledger (one row per client)
```

---

## 7. Strike Selection Mathematics

```
Center Point  =  (Previous Day Nifty Spot Open  +  Previous Day Nifty Spot Close) / 2

ITM Offset (by weekday):
  Monday    →  200 points
  Tuesday   →  100 points   (Expiry Day — tightest strikes)
  Wednesday →  500 points
  Thursday  →  400 points
  Friday    →  300 points

CE Strike  =  round_to_nearest_50( CenterPoint  −  ITM_Offset )
PE Strike  =  round_to_nearest_50( CenterPoint  +  ITM_Offset )

CE contract tracks: Calls below center (bearish premium rising = CE sellers trapped)
PE contract tracks: Puts above center  (puts rising  = PE sellers trapped)
```

---

## 8. Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Async runtime | `asyncio` |
| WebSockets | `websockets` 12.x |
| ORM / DB | SQLAlchemy 2.x + SQLite (or PostgreSQL) |
| Dashboard | Streamlit 1.35+ |
| Charts | Plotly 5.x |
| Data wrangling | Pandas 2.x |
| HTTP client | `aiohttp` |
| 2FA / TOTP | `pyotp` |
| Broker SDKs | `kiteconnect`, `smartapi-python`, `alice_blue` |
