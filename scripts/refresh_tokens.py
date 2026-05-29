#!/usr/bin/env python
"""
scripts/refresh_tokens.py
Daily token auto-refresh worker — runs at 08:30 AM Monday–Friday via cron.

Cron entry (edit with: crontab -e):
    30 8 * * 1-5 /opt/newtraptrading/.venv/bin/python /opt/newtraptrading/scripts/refresh_tokens.py

Broker support matrix:
  • Angel One   — headless TOTP-based re-auth (fully automated)
  • All others  — browser OAuth required; script logs the auth URL so the
                  operator can paste it into their laptop browser via SSH tunnel
"""

from __future__ import annotations

import logging
import os
import sys

# Allow running from any directory — add repo root to path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Load .env before importing project modules so DATABASE_URL and API credentials
# are available — system cron runs with a bare environment that has no .env vars.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables being set externally

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("refresh_tokens")


from database import BrokerName, ClientsRegistry, db_session, get_all_active_clients
from oauth_handler import BROKER_OAUTH_CONFIG, CALLBACK_URL


# ---------------------------------------------------------------------------
# Per-broker refresh handlers
# ---------------------------------------------------------------------------

def _refresh_angel_one(client: ClientsRegistry) -> bool:
    """
    Re-authenticate Angel One using the stored TOTP secret.
    Returns True if token was updated successfully.
    """
    if not client.totp_secret:
        logger.warning(
            "[%s] Angel One TOTP secret not stored — cannot auto-refresh. "
            "Store totp_secret via the Client Management UI.",
            client.name,
        )
        return False

    try:
        from SmartApi import SmartConnect  # type: ignore
        import pyotp
    except ImportError:
        logger.error("[%s] smartapi-python or pyotp not installed.", client.name)
        return False

    try:
        totp    = pyotp.TOTP(client.totp_secret).now()
        obj     = SmartConnect(api_key=client.api_key)
        # generateSession(clientCode, password, totp)
        # api_key = clientCode  |  api_secret = trading password
        data    = obj.generateSession(client.api_key, client.api_secret, totp)
        if not data.get("status"):
            logger.error("[%s] Angel One session failed: %s", client.name, data)
            return False

        jwt_token = (
            data.get("data", {}).get("jwtToken")
            or data.get("data", {}).get("accessToken")
        )
        if not jwt_token:
            logger.error("[%s] No JWT token in Angel One response: %s", client.name, data)
            return False

        with db_session() as s:
            row = s.get(ClientsRegistry, client.id)
            if row:
                row.access_token = jwt_token

        logger.info("[%s] Angel One token refreshed successfully.", client.name)
        return True

    except Exception as exc:
        logger.exception("[%s] Angel One refresh error: %s", client.name, exc)
        return False


def _print_oauth_url(client: ClientsRegistry, broker_key: str) -> None:
    """
    For brokers that require browser OAuth, build and print the auth URL so the
    operator can complete login from their laptop via the SSH tunnel.
    """
    cfg = BROKER_OAUTH_CONFIG.get(broker_key)
    if not cfg:
        logger.warning("[%s] No OAuth config found for broker %s.", client.name, broker_key)
        return

    try:
        auth_url = cfg["auth_url"].format(
            api_key=client.api_key,
            redirect_uri=CALLBACK_URL,
        )
    except KeyError:
        auth_url = cfg.get("auth_url", "")

    logger.warning(
        "[%s] %s requires browser OAuth — auto-refresh not supported.\n"
        "  1. Open an SSH tunnel:  ssh -L 8080:localhost:8080 -N -i key.pem ubuntu@<EC2_IP>\n"
        "  2. Paste this URL in your LOCAL browser:\n"
        "     %s\n"
        "  3. Complete login — the Streamlit Admin Panel will capture the token.",
        client.name, broker_key, auth_url,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_REFRESH_HANDLERS = {
    BrokerName.ANGEL_ONE.value: _refresh_angel_one,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    clients = get_all_active_clients()
    if not clients:
        logger.info("No active clients found — nothing to refresh.")
        return 0

    logger.info("Refreshing tokens for %d active client(s)…", len(clients))

    success_count  = 0
    skipped_count  = 0

    for client in clients:
        broker_key = (
            client.broker.value
            if hasattr(client.broker, "value")
            else str(client.broker)
        )
        handler = _REFRESH_HANDLERS.get(broker_key)
        if handler:
            ok = handler(client)
            if ok:
                success_count += 1
        else:
            _print_oauth_url(client, broker_key)
            skipped_count += 1

    logger.info(
        "Token refresh complete | refreshed=%d skipped(browser-required)=%d",
        success_count, skipped_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
