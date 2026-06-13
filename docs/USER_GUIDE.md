# User Guide — NewTrap Dashboard

---

## Launching the Dashboard

```bash
streamlit run app_ui.py
```

Open `http://localhost:8501` in any browser.

---

## Page 1: Live Trading Monitor (Home)

This is the main real-time dashboard. It auto-refreshes every second.

### Sidebar

| Element | Description |
|---------|-------------|
| Date / Time | Current system clock |
| Market status | Green OPEN / Red CLOSED indicator |
| Morning Anchor section | Today's Center Point, ITM offset, CE/PE strikes and symbols |
| Active Traps list | All ACTIVE_UNMITIGATED traps in the database, colour-coded CE (green) / PE (red) |
| Refresh Clients button | Reloads client list from the database into the execution engine |

### Top Metrics Row

| Metric | What it shows |
|--------|---------------|
| CE Last Price | Most recent live tick on the CE contract |
| PE Last Price | Most recent live tick on the PE contract |
| Active Traps | Count of ACTIVE_UNMITIGATED traps |
| Active Clients | Count of enabled client accounts |
| Center Point | Today's computed center point |

### Retest Flash Banner

When the live premium enters the ±0.5% band around any active trap's entry origin, an **orange flashing banner** appears across the top:

> ⚠️  RETEST SEQUENCE ACTIVE — Premium inside 75-Min Trap Zone. Monitoring 5-Min structure …

This tells you the system is now watching the 5-minute chart for a nested trap to form.

### Candlestick Charts

Two side-by-side Plotly charts display the CE and PE option premium charts.

**Overlay elements on each chart:**

| Element | Color | Meaning |
|---------|-------|---------|
| Blue dashed horizontal line | `#58A6FF` | 75-min trap origin level (where sellers entered short) |
| Orange shaded band | `#f0a500` at 8% opacity | Active retest zone (±0.5% around origin) |
| Green horizontal line | `#238636` | Entry line (5-min sellers' entry — touch trigger level) |
| Red dashed horizontal line | `#DA3633` | Stop-loss boundary (5-min trap candle low) |
| Blue solid horizontal line | `#58A6FF` | Target high (75-min trapped candle high — exit level) |

**Reading the chart:**
1. Blue dashed line = the level to watch for a retest
2. When premium drops into the orange band = retest is active
3. Green line = exact level where the BUY order will fire on touch
4. Red dashed = if a 1-min candle closes below this, position is exited

### Historical Trap Registry Table

Shows the last 30 traps with colour-coded status:

| Status | Color | Meaning |
|--------|-------|---------|
| ACTIVE_UNMITIGATED | Blue | Live, tracking |
| MITIGATED | Green | Target was reached, profit booked |
| VOIDED | Red | SL was hit, position closed |
| EXPIRED_VOID | Grey | Tuesday 15:30 weekly flush |

### Client Portfolio Dashboard

Side-by-side cards for each active client showing:
- Client name and broker
- Active / Inactive status
- Capital allocation
- Current open position symbol (if any)

### Global Master Panic Button

At the bottom of the Client Dashboard section:

1. Click **🚨 SQUARE OFF ALL** — a confirmation prompt appears
2. Click **✅ YES, CLOSE ALL** to immediately fire market SELL orders for every open position across all clients concurrently
3. Click **❌ Cancel** to abort

> Use this in emergency situations only. It sends market orders simultaneously to all active client accounts.

---

## Page 2: Client Management

Navigate via the **sidebar** → **Client Management**.

### Client List Table

Shows all registered clients with columns: ID, Name, Broker, Capital, Status (Active/Inactive), Created date.

**Action buttons per row:**
- **Edit** — opens an inline form to update name, capital, access token, or status
- **Disable / Enable** — toggles the client's `active` flag without deleting the record
- **View Trades** — expands the client's trade history below the table

### Add New Client Form

At the top of the page, expand **"➕ Add New Client"**:

| Field | Required | Description |
|-------|----------|-------------|
| Display Name | Yes | Friendly name (e.g., "Rajan — Zerodha") |
| Broker | Yes | Dropdown: Zerodha / Angel One / Alice Blue / Groww / Upstox / Fyers |
| API Key | Yes | Broker application API key or username |
| API Secret | Yes | Broker secret or password |
| Access Token | Yes | Daily-refreshed bearer / session token |
| TOTP Secret | No | Required for Angel One; base32 TOTP seed |
| Max Capital (₹) | Yes | Maximum capital this client can trade |
| Active | Yes | Toggle to enable/disable on creation |

Click **Save Client** to write to the database. The execution engine will include this client on the next trade trigger.

### Edit Client

Click **Edit** next to any client row. The form pre-fills with current values. Only update fields you want to change. Click **Update Client** to save.

> **Tip:** Use the Edit form every morning to paste the fresh daily access token for each client.

### Client Trade History

Click **View Trades** next to any client. A table expands showing:
- Trade ID, contract symbol, entry price, exit price
- P&L per trade (green for profit, red for loss)
- Exit category (TARGET_HIT / SL_HIT / MANUAL_SQUARE / EXPIRY_VOID)
- Entry and exit timestamps

---

## Page 3: Admin Panel

Navigate via the **sidebar** → **Admin Panel**.

### Morning Initialisation

The **Morning Init** section lets you manually trigger or override the 08:45 AM computation:

| Field | Description |
|-------|-------------|
| Previous Day Open | Nifty spot open from the prior session |
| Previous Day Close | Nifty spot close from the prior session |
| Trade Date | Defaults to today; change to simulate another weekday |

Click **Run Morning Init** to recompute center point and strikes. Results appear immediately and are stored in session state for the engine.

### Manual Trap Entry

For manually logging institutional levels you've identified:

| Field | Description |
|-------|-------------|
| Option Type | CE or PE |
| Strike | Nifty strike (e.g., 23500) |
| Contract Symbol | Full NSE symbol |
| Entry Origin | Premium level where sellers entered short |
| Target High | 75-min trapped candle high |

Click **Register Trap** to write directly to the database as `ACTIVE_UNMITIGATED`.

### Trap Override Controls

For each active trap in the registry:

- **Mark Mitigated** — manually close a trap as target-hit
- **Void Trap** — manually invalidate a trap without firing a trade exit
- **Edit SL Level** — adjust the stop-loss boundary if your read changes

### Expiry Flush

**Tuesday 15:30 PM — Weekly Reset:**

Click **Run Expiry Flush** to immediately mark all remaining `ACTIVE_UNMITIGATED` traps as `EXPIRED_VOID`. This is normally triggered automatically by the engine's `expiry_flush_watchdog` coroutine, but the button lets you trigger it manually.

### Engine Metrics

Live system health indicators:

| Metric | Description |
|--------|-------------|
| Upstox feed status | Last tick received N seconds ago |
| Fyers feed status | Last tick received N seconds ago |
| Active feeder | Which broker is currently serving ticks |
| Tick queue depth | Items waiting in the shared processing queue |
| Open positions | Count of live positions across all clients |
| Today's P&L | Sum of all closed trade P&Ls for the session |

### Database Management

- **Export Trades CSV** — download the trades ledger for the selected date range
- **Export Traps CSV** — download the trap registry
- **View Raw DB Stats** — table row counts for all three tables

---

## Keyboard Shortcuts

Streamlit does not have custom keyboard shortcuts, but these browser shortcuts are useful:

| Shortcut | Action |
|----------|--------|
| `Ctrl + R` / `Cmd + R` | Hard refresh the page |
| `F11` | Full-screen mode for trading terminal view |

---

## Understanding the Color Language

| Color | Code | Meaning across all pages |
|-------|------|--------------------------|
| Blue | `#58A6FF` | Active tracking zones, trap origins, targets |
| Green | `#238636` | Bullish / CE contracts, profits, mitigated traps |
| Red | `#DA3633` | Bearish / PE contracts, losses, voided traps, SL lines |
| Orange | `#f0a500` | Retest zone alert, pending confirmation |
| Grey | `#8B949E` | Inactive / expired items, secondary info |

---

## Frequently Asked Questions

**Q: The charts show no data. Why?**

A: The engine (`main.py`) must be running and connected to a broker WebSocket to stream tick data. The dashboard reads from the shared database — live bar data is pushed into the UI by the running engine.

**Q: How do I add the ATM contract for execution if my tracked contract is ITM?**

A: The system fires orders on the **At-The-Money (ATM)** contract at the time of entry, not the tracked ITM contract. The tracked ITM CE/PE chart is only used for structural analysis (trap detection). The ATM contract for the execution order is the one closest to the current spot price at the moment of the touch trigger.

**Q: What happens if both Upstox and Fyers feeds go down?**

A: Both feeders have exponential-backoff reconnect loops (2s → 4s → 8s → 16s → 30s). The `DualFeederSupervisor` keeps retrying both connections. No orders will fire while the tick queue is empty, which protects you from ghost signals.

**Q: Can I run the dashboard without the trading engine?**

A: Yes. The dashboard reads from the SQLite database and can display historical data without the engine running. You won't get live tick updates, but the trap registry, trade history, and client management are fully functional.

**Q: How do I simulate a trading day without real broker connections?**

A: Set the `DATABASE_URL` to a test database, use the Admin Panel to manually register traps, and mock the tick queue by injecting test tick records directly into the `TICK_QUEUE` asyncio queue. You can also replay a CSV of historical ticks by writing a simple feeder that reads the CSV and pushes records into the queue.
