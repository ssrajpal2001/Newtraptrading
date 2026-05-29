# AWS EC2 Deployment Guide

This guide covers every infrastructure layer needed to run NewTrap reliably on a remote AWS EC2 instance, including the SSH tunnel trick that makes browser-based OAuth work from your laptop against a cloud server.

---

## Architecture Overview

```
Your Laptop
  ├── Browser (http://EC2-IP:8501) ──── views the Streamlit dashboard
  ├── Browser (OAuth login page)   ──── redirects to localhost:8080
  └── SSH Client
        └── Tunnel: laptop:8080 ──────► EC2:8080   (OAuth callback)

AWS EC2 Instance (t3.medium, Ubuntu 22.04)
  ├── newtrap-engine.service   (systemd daemon, always running)
  ├── newtrap-ui.service       (Streamlit on port 8501, always running)
  ├── Chrony NTP daemon        (clock sync against 169.254.169.123)
  └── SQLite DB                (/opt/newtraptrading/newtrap_trading.db)
```

---

## Step 1: Launch the EC2 Instance

### Recommended specification

| Property | Value |
|----------|-------|
| Instance type | `t3.medium` (2 vCPU, 4 GB RAM) or `t3.large` for heavier loads |
| AMI | Ubuntu Server 22.04 LTS (64-bit x86) |
| Storage | 20 GB gp3 SSD |
| Key pair | Create or select an existing `.pem` key |

### Security Group — Inbound Rules

Configure these rules in the AWS Console under **EC2 → Security Groups → Inbound Rules**:

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | **Your IP only** (`x.x.x.x/32`) | SSH console access |
| 8501 | TCP | **Your IP only** | Streamlit dashboard |
| 8080 | — | **Do not open** | Kept closed; OAuth uses SSH tunnel |

> Restricting to your IP is critical. Never open port 22 or 8501 to `0.0.0.0/0`.

---

## Step 2: SSH Tunnel for OAuth Browser Login

When you click **"Connect"** in the dashboard (Admin Panel or Client Management), the system opens your default browser to the broker login page. After you log in, the broker redirects to `http://localhost:8080/?code=AUTH_CODE`.

On your local laptop, `localhost:8080` doesn't know about the EC2 server unless you create an SSH tunnel. The tunnel tells your laptop: "any traffic to my port 8080 should be forwarded through SSH to port 8080 on the EC2 instance."

### How to set up the tunnel

Open a dedicated terminal on your laptop and run:

```bash
ssh -L 8080:localhost:8080 -N -i /path/to/your-aws-key.pem ubuntu@YOUR_EC2_PUBLIC_IP
```

| Flag | Meaning |
|------|---------|
| `-L 8080:localhost:8080` | Forward laptop port 8080 → EC2 port 8080 |
| `-N` | Don't open a shell — tunnel only |
| `-i your-aws-key.pem` | Your EC2 key pair private key |

Keep this terminal open the entire time you need OAuth. You can open a separate SSH session for regular commands.

### How the full OAuth flow works end-to-end

```
1. You click "Connect" in the NewTrap dashboard (running on EC2, viewed on laptop browser)
2. EC2 starts the OAuth callback server on EC2:8080
3. EC2 calls webbrowser.open() ← this opens on YOUR laptop via the X11/browser session
   (for headless EC2 you click the printed URL manually — see note below)
4. You authenticate in your laptop's browser
5. Broker redirects to http://localhost:8080/?code=AUTH_CODE
6. Your laptop's port 8080 receives it
7. SSH tunnel forwards it to EC2:8080
8. EC2's OAuth callback server captures the code, exchanges it for a token
9. Token is saved to the database automatically
```

> **Headless EC2 note:** EC2 instances have no display, so `webbrowser.open()` will not literally open a browser on the server. Instead, the Admin Panel and Client Management pages print the full broker authorization URL as a clickable link. Copy and paste that URL into your laptop's browser manually to complete the login. The SSH tunnel will carry the callback back to EC2 regardless.

### Convenience: persistent tunnel with autossh

```bash
# Install autossh (reconnects the tunnel if it drops)
sudo apt-get install autossh   # on your laptop (macOS: brew install autossh)

# Run persistent tunnel
autossh -M 0 -f -N \
  -L 8080:localhost:8080 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -i /path/to/your-key.pem \
  ubuntu@YOUR_EC2_PUBLIC_IP
```

---

## Step 3: Provision the EC2 Instance

SSH into the instance and run the setup script:

```bash
ssh -i your-key.pem ubuntu@YOUR_EC2_PUBLIC_IP

# Clone the repo
git clone https://github.com/ssrajpal2001/Newtraptrading.git
cd Newtraptrading

# Make the script executable and run it
chmod +x deploy/setup_ec2.sh
./deploy/setup_ec2.sh
```

The script handles:
- OS updates and Python 3.11 installation
- Chrony NTP configuration (clock drift prevention)
- Virtual environment creation and dependency installation
- Database initialisation
- systemd service installation

---

## Step 4: Configure Credentials

```bash
nano /opt/newtraptrading/.env
```

Fill in all broker credentials. Save and exit.

---

## Step 5: Start Services

```bash
sudo systemctl start newtrap-engine
sudo systemctl start newtrap-ui

# Verify both are running
sudo systemctl status newtrap-engine
sudo systemctl status newtrap-ui
```

---

## Step 6: Access the Dashboard

Open your laptop's browser and navigate to:

```
http://YOUR_EC2_PUBLIC_IP:8501
```

---

## Clock Drift Prevention (Chrony NTP)

AWS EC2 virtual machines can experience "clock drift" — the system clock slowly deviates from real time. Even 1–2 seconds of drift causes bar boundaries to misalign with exchange timestamps, producing late entries or phantom signals.

### Why AWS time sync matters

- NSE option tick data is timestamped in IST (UTC+5:30)
- Your bar aggregator calculates 1-min, 5-min, and 75-min boundaries using the system clock
- A 3-second drift = wrong bar assignment = entry on the wrong candle

### Chrony configuration

The setup script installs `deploy/chrony.conf` which:

1. Uses `169.254.169.123` — the AWS Time Sync Service link-local address available on every EC2 instance. It synchronises to GPS-disciplined atomic clocks in your AWS region.
2. Enables `makestep 0.1 3` — corrects large offsets only at startup, then slews (gradually adjusts) to avoid time jumps during a live trading session.
3. Enables `rtcsync` — keeps the hardware clock aligned with system time.

### Verify time sync is working

```bash
chronyc tracking
```

Expected output:
```
Reference ID    : A9FEA97B (169.254.169.123)
Stratum         : 4
System time     : 0.000012345 seconds fast of NTP time
Last offset     : +0.000003241 seconds
RMS offset      : 0.000004123 seconds
Frequency       : -5.321 ppm fast
Residual freq   : +0.001 ppm
Skew            : 0.123 ppm
Root delay      : 0.000123456 seconds
Root dispersion : 0.000234567 seconds
Update interval : 16.4 seconds
Leap status     : Normal
```

Target: **System time offset < 1 millisecond**, Leap status: **Normal**.

---

## Persistent Execution (systemd)

Without systemd, the engine dies the moment you close your SSH session:

```
SSH session closes
  └─► your shell exits
        └─► all child processes (python main.py, streamlit) receive SIGHUP
              └─► both processes die
                    └─► open positions have no risk manager
```

With systemd:

```
SSH session closes
  └─► newtrap-engine.service keeps running independently
        └─► engine monitors risk, fires SL exits, runs expiry flush
              └─► Restart=on-failure auto-recovers from crashes
```

### Key systemd commands

```bash
# View live engine logs
sudo journalctl -u newtrap-engine -f

# View live dashboard logs
sudo journalctl -u newtrap-ui -f

# Restart after editing .env or code
sudo systemctl restart newtrap-engine newtrap-ui

# Stop all trading activity
sudo systemctl stop newtrap-engine newtrap-ui

# Check if auto-start on reboot is enabled
sudo systemctl is-enabled newtrap-engine newtrap-ui
```

---

## tmux Alternative (Development / Testing)

If you prefer not to use systemd during testing:

```bash
# Install tmux
sudo apt-get install -y tmux

# Create a persistent session
tmux new-session -d -s trading

# Run the engine in a window
tmux send-keys -t trading "cd /opt/newtraptrading && source .venv/bin/activate && python main.py" Enter

# Open a second window for the dashboard
tmux new-window -t trading
tmux send-keys -t trading "cd /opt/newtraptrading && source .venv/bin/activate && streamlit run app_ui.py" Enter

# Detach and close your SSH terminal — processes keep running
tmux detach

# Re-attach later
tmux attach -t trading
```

---

## Monitoring and Alerting

### Check if processes are alive

```bash
sudo systemctl status newtrap-engine newtrap-ui
```

### Tail recent log errors

```bash
sudo journalctl -u newtrap-engine --since "1 hour ago" | grep -i error
```

### Disk space (SQLite DB growth)

```bash
du -sh /opt/newtraptrading/newtrap_trading.db
```

### Reboot-safe check

After a system reboot, both services should start automatically:

```bash
sudo reboot
# wait ~30 seconds
ssh -i your-key.pem ubuntu@YOUR_EC2_PUBLIC_IP
sudo systemctl status newtrap-engine newtrap-ui
```

---

## Cost Estimation

| Resource | Spec | Monthly cost (approx.) |
|----------|------|----------------------|
| EC2 t3.medium | 2 vCPU, 4 GB, On-Demand | ~$30 USD |
| EC2 t3.medium | Reserved 1-year | ~$18 USD |
| EBS gp3 20 GB | Storage | ~$1.60 USD |
| Data transfer | Minimal (WebSocket ticks inbound) | ~$0–2 USD |

> The engine only needs to run during market hours (09:15–15:30 IST, Mon–Fri). An EC2 scheduler that starts/stops the instance outside these hours reduces costs by ~70%.

---

## Security Hardening Checklist

- [ ] SSH key pair in use (no password authentication)
- [ ] Security Group: port 22 restricted to your IP only
- [ ] Security Group: port 8501 restricted to your IP only
- [ ] Security Group: port 8080 NOT open externally
- [ ] `.env` file permissions: `chmod 600 /opt/newtraptrading/.env`
- [ ] No credentials in source code or git history
- [ ] `authorized_keys` contains only your public key
- [ ] OS packages updated: `sudo apt-get update && sudo apt-get upgrade`
- [ ] Fail2Ban installed to block brute-force SSH attempts:
  ```bash
  sudo apt-get install -y fail2ban
  sudo systemctl enable fail2ban
  ```
