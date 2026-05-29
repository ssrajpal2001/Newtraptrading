# Setup & Deployment Guide

---

## 1. Prerequisites

- Python 3.11 or higher
- pip / virtualenv
- NSE F&O trading accounts with API access for at least one data broker (Upstox or Fyers)
- Client broker accounts (Zerodha, Angel One, Alice Blue, or Groww) for execution

---

## 2. Installation

```bash
# Clone the repository
git clone https://github.com/ssrajpal2001/Newtraptrading.git
cd Newtraptrading

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows

# Install all dependencies
pip install -r requirements.txt
```

Install broker-specific SDKs only for the brokers you actually use:

```bash
pip install kiteconnect            # Zerodha
pip install smartapi-python pyotp  # Angel One
pip install alice_blue             # Alice Blue
# Groww uses aiohttp (already in requirements.txt)
```

---

## 3. Environment Configuration

```bash
cp .env.example .env
```

Open `.env` and fill in the values:

```env
# ---- Data Feeders ----
UPSTOX_API_KEY=your_upstox_api_key
UPSTOX_API_SECRET=your_upstox_api_secret
UPSTOX_ACCESS_TOKEN=your_daily_access_token

FYERS_APP_ID=your_fyers_app_id
FYERS_SECRET_KEY=your_fyers_secret
FYERS_ACCESS_TOKEN=your_daily_access_token

# ---- Database ----
DATABASE_URL=sqlite:///newtrap_trading.db
```

---

## 4. Upstox API Setup

1. Log in to [Upstox Developer Console](https://developer.upstox.com/)
2. Create a new application → note the **API Key** and **Secret**
3. Set the redirect URI to `http://localhost:8080/callback`
4. Each trading day, generate a fresh access token:
   ```
   GET https://api.upstox.com/v2/login/authorization/dialog
       ?response_type=code
       &client_id=YOUR_API_KEY
       &redirect_uri=http://localhost:8080/callback
   ```
   Exchange the `code` for an access token via:
   ```
   POST https://api.upstox.com/v2/login/authorization/token
   ```
5. Paste the token into `UPSTOX_ACCESS_TOKEN` in `.env`

> **Token refresh tip:** Upstox access tokens expire daily. Automate the refresh using the Upstox Login API or a Selenium script that runs at 08:30 AM.

---

## 5. Fyers API Setup

1. Log in to [Fyers API](https://myapi.fyers.in/)
2. Create an app → note **App ID** and **Secret Key**
3. Set redirect URI to `http://localhost:8080/`
4. Each day, generate the auth token using the Fyers auth flow
5. Paste into `FYERS_ACCESS_TOKEN` in `.env`

---

## 6. Adding Client Broker Accounts

Client accounts are managed through the **Client Management** page in the dashboard UI. You can also insert them directly via the database helper:

```python
from database import init_db, db_session, ClientsRegistry, BrokerName

init_db()

with db_session() as s:
    s.add(ClientsRegistry(
        name="Client A",
        broker=BrokerName.ZERODHA,
        api_key="your_kite_api_key",
        api_secret="your_kite_api_secret",
        access_token="daily_kite_access_token",
        max_capital=500000.0,
        active=True,
    ))
```

Or use the **Client Management** page in the Streamlit dashboard for a full form-based interface.

---

## 7. Zerodha Kite Setup (per client)

1. Create a Kite Connect developer app at [kite.trade](https://kite.trade/)
2. Note **API Key** and **API Secret**
3. Each day generate a session token:
   ```python
   from kiteconnect import KiteConnect
   kite = KiteConnect(api_key="your_api_key")
   # Open kite.login_url() in browser, login, copy the request_token
   data = kite.generate_session("request_token", api_secret="your_secret")
   access_token = data["access_token"]
   ```
4. Update the client's `access_token` in the Client Management UI or DB

---

## 8. Angel One SmartAPI Setup (per client)

1. Register at [Angel One SmartAPI](https://smartapi.angelbroking.com/)
2. Get your **API Key**
3. Enable TOTP in your Angel One account settings — save the TOTP secret
4. Credentials to enter:
   - `api_key` = SmartAPI key
   - `access_token` = client's Angel One login password (used in session generation)
   - `totp_secret` = TOTP seed (base32 string from QR code)

---

## 9. Alice Blue ANT Setup (per client)

1. Log in to [Alice Blue ANT API portal](https://a3.aliceblueonline.com/)
2. Generate API key and password
3. Credentials:
   - `api_key` = Alice Blue user ID
   - `api_secret` = Alice Blue API password
   - `access_token` = generated session auth token

---

## 10. Database Initialisation

```bash
python -c "from database import init_db; init_db()"
```

This creates the SQLite database file `newtrap_trading.db` with all three tables.

To use PostgreSQL instead:

```env
DATABASE_URL=postgresql+psycopg2://username:password@localhost:5432/newtrap
```

Install the driver:
```bash
pip install psycopg2-binary
```

---

## 11. Starting the System

### Development (single machine)

```bash
# Terminal 1: Trading engine
python main.py

# Terminal 2: Dashboard
streamlit run app_ui.py
```

### Production (server deployment)

**Engine as a systemd service:**

```ini
# /etc/systemd/system/newtrap-engine.service
[Unit]
Description=NewTrap Trading Engine
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/newtraptrading
ExecStart=/opt/newtraptrading/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
EnvironmentFile=/opt/newtraptrading/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable newtrap-engine
sudo systemctl start newtrap-engine
```

**Dashboard behind nginx:**

```bash
# Start streamlit on a fixed port
streamlit run app_ui.py --server.port 8501 --server.headless true
```

```nginx
# nginx config
location /newtrap/ {
    proxy_pass http://127.0.0.1:8501;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

---

## 12. Daily Token Refresh Automation

Since most Indian broker access tokens expire daily, schedule a token refresh script at **08:30 AM** before the market opens:

```python
# refresh_tokens.py — customise per broker
import os
from database import db_session, ClientsRegistry

def refresh_all_tokens():
    # Call each broker's login API to regenerate access_token
    # Update the database records via the Client Management UI or directly:
    with db_session() as s:
        client = s.query(ClientsRegistry).filter_by(name="Client A").first()
        if client:
            client.access_token = fetch_new_token(client)
```

Add to crontab:
```
30 8 * * 1-5 /opt/newtraptrading/.venv/bin/python /opt/newtraptrading/refresh_tokens.py
```
