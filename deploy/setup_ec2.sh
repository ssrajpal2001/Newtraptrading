#!/usr/bin/env bash
# deploy/setup_ec2.sh
#
# One-shot provisioning script for a fresh AWS EC2 Ubuntu 22.04 instance.
# Run as the 'ubuntu' user after first SSH login:
#
#   chmod +x deploy/setup_ec2.sh
#   ./deploy/setup_ec2.sh
#
# What it does:
#   1. Updates the OS and installs system dependencies
#   2. Installs Python 3.11 and pip
#   3. Configures Chrony for NTP time synchronisation against AWS time servers
#   4. Clones / copies the application to /opt/newtraptrading
#   5. Creates the Python virtual environment and installs requirements
#   6. Installs and enables both systemd services
#   7. Prints the SSH tunnel command to use from your local laptop

set -euo pipefail

APP_DIR="/opt/newtraptrading"
REPO_URL="https://github.com/ssrajpal2001/Newtraptrading.git"
SERVICE_USER="ubuntu"

echo "============================================================"
echo "  NewTrap EC2 Setup Script"
echo "============================================================"

# -------------------------------------------------------------------
# 1. System update
# -------------------------------------------------------------------
echo "[1/7] Updating system packages…"
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y \
    git \
    curl \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    chrony \
    tmux \
    htop \
    jq

# -------------------------------------------------------------------
# 2. Configure Chrony for AWS time sync (clock drift prevention)
# -------------------------------------------------------------------
echo "[2/7] Configuring Chrony NTP sync…"
sudo tee /etc/chrony/chrony.conf > /dev/null <<'CHRONYCONF'
# AWS-recommended NTP server (169.254.169.123 = AWS Time Sync Service)
server 169.254.169.123 prefer iburst

# Fallback public NTP pools
pool 0.ubuntu.pool.ntp.org iburst
pool 1.ubuntu.pool.ntp.org iburst
pool 2.ubuntu.pool.ntp.org iburst
pool 3.ubuntu.pool.ntp.org iburst

keyfile /etc/chrony/chrony.keys
driftfile /var/lib/chrony/chrony.drift
logdir /var/log/chrony
maxupdateskew 100.0
rtcsync
makestep 1 3
CHRONYCONF

sudo systemctl enable chrony
sudo systemctl restart chrony
sleep 2
echo "  Chrony status:"
chronyc tracking | grep -E "Reference|System time|Last offset"

# -------------------------------------------------------------------
# 3. Clone / update the application
# -------------------------------------------------------------------
echo "[3/7] Setting up application directory at $APP_DIR…"
if [ -d "$APP_DIR/.git" ]; then
    echo "  Existing repo found — pulling latest…"
    cd "$APP_DIR"
    git pull origin main
else
    sudo git clone "$REPO_URL" "$APP_DIR"
    sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
fi

# -------------------------------------------------------------------
# 4. Python virtual environment
# -------------------------------------------------------------------
echo "[4/7] Creating Python virtual environment…"
cd "$APP_DIR"
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt

echo "  Optional broker SDKs (uncomment as needed):"
echo "  .venv/bin/pip install kiteconnect smartapi-python pyotp alice_blue"

# -------------------------------------------------------------------
# 5. Environment file
# -------------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "  ⚠️  .env file created from template."
    echo "  Edit $APP_DIR/.env and add your broker credentials before starting services."
    echo ""
fi

# -------------------------------------------------------------------
# 6. Initialise database
# -------------------------------------------------------------------
echo "[5/7] Initialising database…"
cd "$APP_DIR"
.venv/bin/python -c "from database import init_db; init_db()"

# -------------------------------------------------------------------
# 7. Install systemd services
# -------------------------------------------------------------------
echo "[6/7] Installing systemd services…"
sudo cp "$APP_DIR/deploy/newtrap-engine.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/newtrap-ui.service"     /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable newtrap-engine newtrap-ui

echo ""
echo "  Services installed but NOT started yet."
echo "  Edit your .env credentials first, then run:"
echo "    sudo systemctl start newtrap-engine"
echo "    sudo systemctl start newtrap-ui"

# -------------------------------------------------------------------
# 8. Print usage summary
# -------------------------------------------------------------------
EC2_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<your-ec2-ip>")

echo ""
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "  NEXT STEPS:"
echo ""
echo "  1. Edit credentials:"
echo "     nano $APP_DIR/.env"
echo ""
echo "  2. Start services:"
echo "     sudo systemctl start newtrap-engine newtrap-ui"
echo ""
echo "  3. View logs:"
echo "     sudo journalctl -u newtrap-engine -f"
echo "     sudo journalctl -u newtrap-ui -f"
echo ""
echo "  4. SSH tunnel for OAuth browser login (run on YOUR LAPTOP):"
echo "     ssh -L 8080:localhost:8080 -N -i your-key.pem ubuntu@$EC2_IP"
echo ""
echo "  5. Open the dashboard (in your laptop's browser):"
echo "     http://$EC2_IP:8501"
echo ""
echo "  ⚠️  Ensure your EC2 Security Group has:"
echo "     - Port 22   open to your IP (SSH)"
echo "     - Port 8501 open to your IP (Streamlit dashboard)"
echo "     - Port 8080 CLOSED externally (OAuth handled via SSH tunnel)"
echo ""
