# NewTrap — Institutional Option-Chart Algorithmic Trading Platform

> **Option-chart-centric institutional liquidity scanner, multi-client risk manager, and historical trap registry for Nifty weekly options (Tuesday Expiry, NSE).**

---

## Table of Contents

1. [What This System Does](#what-this-system-does)
2. [System Architecture](#system-architecture)
3. [Module Overview](#module-overview)
4. [Complete Trading Flow](#complete-trading-flow)
5. [Quick Start](#quick-start)
6. [Running the Application](#running-the-application)
7. [Application Pages](#application-pages)
8. [Configuration Reference](#configuration-reference)
9. [Broker Setup](#broker-setup)
10. [Database Schema](#database-schema)
11. [Detailed Documentation](#detailed-documentation)

---

## What This System Does

NewTrap scans **Nifty weekly option premium charts** (not the spot index) to detect when institutional sellers are trapped above their stop-loss. When that happens, the system:

1. Registers the precise premium level where sellers entered short trades into a database.
2. Waits for the premium to cool back down to that level (retest).
3. Confirms a smaller 5-minute nested seller trap at the same zone.
4. The instant the live premium touches the 5-minute sellers' entry line — fires a **market BUY** simultaneously across every configured client account.
5. Manages the trade with a 1-minute candle-close stop-loss filter and targets the original 75-minute trapped candle's high.

---

## System Architecture

```
                    ┌─────────────────────────────────────┐
                    │     08:45 AM MORNING ENGINE         │
                    │  • Previous Day Spot OHLC fetch     │
                    │  • Center Point calculation         │
                    │  • ITM Distance Matrix lookup       │
                    │  • CE / PE strike assignment        │
                    └──────────────┬──────────────────────┘
                                   │
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   DUAL BROKER DATA FEEDER LAYER     │
                    │  Primary:  Upstox WebSocket         │
                    │  Fallback: Fyers WebSocket          │
                    │  Failover: < 100ms heartbeat check  │
                    └──────────────┬──────────────────────┘
                                   │  raw ticks → shared queue
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   MULTI-TIMEFRAME BAR AGGREGATOR    │
                    │  • 75-Min HTF bars (CE & PE charts) │
                    │  • 5-Min  LTF bars                  │
                    │  • 1-Min  Risk bars                 │
                    └──────────────┬──────────────────────┘
                                   │  closed bars
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   TRAP DETECTION STATE MACHINE      │
                    │  • HTF bearish candle detection     │
                    │  • High SL breach = trap confirmed  │
                    │  • Retest zone monitoring           │
                    │  • 5-Min nested trap confirmation   │
                    │  • Touch entry trigger              │
                    │  • 1-Min SL close guard             │
                    └──────────────┬──────────────────────┘
                                   │  entry / exit signals
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   MULTI-CLIENT EXECUTION ENGINE     │
                    │  • Zerodha Kite                     │
                    │  • Angel One SmartAPI               │
                    │  • Alice Blue ANT API               │
                    │  • Groww API                        │
                    │  • asyncio.gather parallel dispatch │
                    └──────────────┬──────────────────────┘
                                   │
                                   ▼
                    ┌─────────────────────────────────────┐
                    │   STREAMLIT DASHBOARD (4 pages)     │
                    │  • Live Trading Monitor             │
                    │  • Client Management                │
                    │  • Admin Panel                      │
                    │  • Trade History                    │
                    └─────────────────────────────────────┘
```

---

## Module Overview

| File | Purpose |
|------|---------|
| `config.py` | Morning anchor maths, ITM matrix, DayConfig, bar-key helpers |
| `database.py` | SQLAlchemy ORM — 3 tables + full trap / trade lifecycle functions |
| `data_feeder.py` | Async WebSocket feeders, bar aggregator, TrapDetector state machine |
| `execution_engine.py` | Broker adapters + async parallel order router + panic square-off |
| `oauth_handler.py` | Browser OAuth2 callback interceptor for all 6 brokers |
| `app_ui.py` | Main Streamlit trading dashboard (live charts, trap registry) |
| `pages/1_Client_Management.py` | Full client CRUD + per-client OAuth connect button |
| `pages/2_Admin_Panel.py` | Feeder OAuth, morning init, trap overrides, flush, export |
| `main.py` | Async orchestrator with graceful shutdown |
| `deploy/newtrap-engine.service` | systemd daemon for the trading engine |
| `deploy/newtrap-ui.service` | systemd daemon for the Streamlit dashboard |
| `deploy/setup_ec2.sh` | One-shot AWS EC2 Ubuntu provisioning script |
| `deploy/chrony.conf` | Precision NTP config for EC2 clock drift prevention |

---

## Complete Trading Flow

```
[08:45 AM] Spot OHLC fetched
     │
     ▼
CenterPoint = (prev_open + prev_close) / 2
ITM offset  = {Wed:500, Thu:400, Fri:300, Mon:200, Tue:100}
CE strike   = round(CenterPoint - offset, 50)   ← tracks bearish premium
PE strike   = round(CenterPoint + offset, 50)   ← tracks bullish premium
     │
     ▼
[STREAMING: 75-Min Option Premium Bars]
     │
     ├─► If bar.close < bar.open  →  mark as bearish candle (seller setup)
     │
     └─► If next bar.high > bearish_candle.high
              → HIGH SL BREACHED = SELLERS TRAPPED
              → Register trap in DB (status: ACTIVE_UNMITIGATED)
                  entry_origin = bearish_candle.open
                  target_high  = current_bar.high
     │
     ▼
[RETEST MONITORING]
Is live premium within ±0.5% of entry_origin?
     │
     ├─► NO  → Keep tracking
     │
     └─► YES → Enter 5-Min option chart watch
     │
     ▼
[5-MIN CHART WATCH]
     │
     ├─► Identify 5-min bearish candle inside the zone
     │
     └─► 5-min bar.high > 5-min bearish candle.high
              → 5-MIN NESTED TRAP CONFIRMED
              → Record: ltf_entry_line = 5min_bearish.open
                        ltf_sl_line    = 5min_bearish.low
     │
     ▼
[LIVE TICK ENGINE]
Does live premium ≤ ltf_entry_line?
     │
     ├─► NO  → Wait for touch
     │
     └─► YES → ⚡ FIRE MARKET BUY (ATM contract, all clients in parallel)
     │
     ▼
[RISK SUPERVISOR — every 1-min bar close]
Does 1-min candle CLOSE < ltf_sl_line?
     │
     ├─► YES → Void trap in DB
     │         Fire market SELL on all clients
     │         Cascade to next lower ACTIVE_UNMITIGATED trap layer
     │
     └─► NO  → Has premium hit target_high?
                   YES → Mitigate trap in DB, fire market SELL, book profit
                   NO  → Hold position

[TUESDAY 15:30]
All remaining ACTIVE_UNMITIGATED traps → EXPIRED_VOID
```

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/ssrajpal2001/Newtraptrading.git
cd Newtraptrading
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Upstox and Fyers credentials
```

### 3. Initialise the database

```bash
python -c "from database import init_db; init_db()"
```

### 4. Start the dashboard

```bash
streamlit run app_ui.py
```

### 5. Start the trading engine (separate terminal)

```bash
python main.py
```

---

## Running the Application

### Dashboard only (monitoring without live trading)

```bash
streamlit run app_ui.py
```

Access at `http://localhost:8501`

### Full engine + dashboard

Run these in two separate terminals:

**Terminal 1 — Trading engine:**
```bash
python main.py
```

**Terminal 2 — Dashboard:**
```bash
streamlit run app_ui.py
```

The engine writes to the SQLite database; the dashboard reads from the same database and auto-refreshes every second.

---

## Application Pages

| Page | URL path | Purpose |
|------|----------|---------|
| Live Trading Monitor | `/` (root) | Real-time CE/PE candlestick charts, trap overlays, retest flash |
| Client Management | `/?page=Client_Management` | Add, edit, disable clients and view their trade history |
| Admin Panel | `/?page=Admin_Panel` | Morning init, manual trap entry, expiry flush, engine metrics |

Navigate between pages using the **sidebar navigation** in Streamlit.

---

## Configuration Reference

All environment variables — copy `.env.example` to `.env`:

| Variable | Description |
|----------|-------------|
| `UPSTOX_API_KEY` | Upstox application API key |
| `UPSTOX_API_SECRET` | Upstox application secret |
| `UPSTOX_ACCESS_TOKEN` | Daily-refreshed Upstox bearer token |
| `FYERS_APP_ID` | Fyers application ID |
| `FYERS_SECRET_KEY` | Fyers secret key |
| `FYERS_ACCESS_TOKEN` | Daily-refreshed Fyers bearer token |
| `DATABASE_URL` | SQLAlchemy connection string (default: `sqlite:///newtrap_trading.db`) |

---

## Broker Setup

See [`docs/SETUP.md`](docs/SETUP.md) for step-by-step credential setup for each supported broker.

---

## Database Schema

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full schema and state-transition diagrams.

---

## Detailed Documentation

| Document | Contents |
|----------|----------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | DB schema, state machines, module dependency graph |
| [`docs/SETUP.md`](docs/SETUP.md) | Broker credential setup, OAuth token flow, local deployment |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | Page-by-page UI walkthrough (Live Monitor, Client Mgmt, Admin Panel) |
| [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) | EC2 provisioning, SSH tunnel for OAuth, systemd services, Chrony NTP |
