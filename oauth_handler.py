"""
oauth_handler.py
Browser-based OAuth2 callback interceptor for all supported brokers.

Flow
----
1. Caller provides broker name, client_id, api_key, api_secret, and the
   broker's authorization URL template.
2. A lightweight HTTP server starts on localhost:8080 in a background thread.
3. webbrowser.open() launches the broker login portal in the user's default
   browser.
4. After the user authenticates, the broker redirects to
   http://localhost:8080/?code=AUTH_CODE
5. The server captures the code, exchanges it for an access_token via a POST
   request, stores the token in the database, and shuts down.

All broker-specific URL templates and token exchange endpoints are defined
in BROKER_OAUTH_CONFIG below.  Add new brokers by extending that dict.
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from database import BrokerName, ClientsRegistry, db_session

logger = logging.getLogger(__name__)

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8080
CALLBACK_URL  = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}"

# Maximum seconds to wait for the user to complete the browser login
OAUTH_TIMEOUT_SECS = 300


# ---------------------------------------------------------------------------
# Broker-specific OAuth configuration
# ---------------------------------------------------------------------------

BROKER_OAUTH_CONFIG: Dict[str, Dict[str, Any]] = {
    # ------------------------------------------------------------------ Upstox
    BrokerName.UPSTOX.value: {
        "auth_url": (
            "https://api.upstox.com/v2/login/authorization/dialog"
            "?response_type=code"
            "&client_id={api_key}"
            "&redirect_uri={redirect_uri}"
        ),
        "token_url":    "https://api.upstox.com/v2/login/authorization/token",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "code":          code,
            "client_id":     api_key,
            "client_secret": api_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        },
        "token_field": "access_token",
    },
    # ------------------------------------------------------------------- Fyers
    BrokerName.FYERS.value: {
        "auth_url": (
            "https://api.fyers.in/api/v2/generate-authcode"
            "?client_id={api_key}"
            "&redirect_uri={redirect_uri}"
            "&response_type=code"
            "&state=newtrap"
        ),
        "token_url":    "https://api.fyers.in/api/v2/validate-authcode",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "grant_type":    "authorization_code",
            "appIdHash":     _sha256_hash(f"{api_key}:{api_secret}"),
            "code":          code,
        },
        "token_field": "access_token",
    },
    # ----------------------------------------------------------------- Zerodha
    BrokerName.ZERODHA.value: {
        "auth_url": (
            "https://kite.trade/connect/login"
            "?api_key={api_key}"
            "&v=3"
        ),
        "token_url":    "https://api.kite.trade/session/token",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "api_key":      api_key,
            "request_token": code,
            "checksum":     _sha256_hash(f"{api_key}{code}{api_secret}"),
        },
        "token_field": "access_token",
    },
    # -------------------------------------------------------------- Angel One
    BrokerName.ANGEL_ONE.value: {
        "auth_url": (
            "https://smartapi.angelbroking.com/publisher-login"
            "?api_key={api_key}"
        ),
        "token_url":    "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "clientcode": api_key,
            "password":   api_secret,
            "totp":       code,           # Angel uses TOTP as the "code"
        },
        "token_field": "jwtToken",
    },
    # ------------------------------------------------------------- Alice Blue
    BrokerName.ALICE_BLUE.value: {
        "auth_url": (
            "https://ant.aliceblueonline.com/oauth2/auth"
            "?response_type=code"
            "&client_id={api_key}"
            "&redirect_uri={redirect_uri}"
        ),
        "token_url":    "https://ant.aliceblueonline.com/oauth2/token",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  redirect_uri,
            "client_id":     api_key,
            "client_secret": api_secret,
        },
        "token_field": "access_token",
    },
    # ------------------------------------------------------------------- Groww
    BrokerName.GROWW.value: {
        "auth_url": (
            "https://groww.in/open-account"
            "?client_id={api_key}"
            "&response_type=code"
            "&redirect_uri={redirect_uri}"
        ),
        "token_url":    "https://api.groww.in/v1/oauth/token",
        "token_method": "POST",
        "token_payload": lambda code, api_key, api_secret, redirect_uri: {
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     api_key,
            "client_secret": api_secret,
            "redirect_uri":  redirect_uri,
        },
        "token_field": "access_token",
    },
}


# ---------------------------------------------------------------------------
# SHA-256 helper (used by Zerodha / Fyers checksum)
# ---------------------------------------------------------------------------

def _sha256_hash(data: str) -> str:
    import hashlib
    return hashlib.sha256(data.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Callback HTTP handler
# ---------------------------------------------------------------------------

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Captures ?code= from the broker redirect and stores it on the server."""

    captured_code: Optional[str] = None
    shutdown_flag: threading.Event = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code   = (params.get("code") or params.get("auth_code") or [None])[0]

        if code:
            _OAuthCallbackHandler.captured_code = code
            body = (
                b"<html><body style='background:#0D1117;color:#58A6FF;"
                b"font-family:monospace;text-align:center;padding-top:80px'>"
                b"<h2>Authentication successful.</h2>"
                b"<p>You may close this tab and return to the NewTrap dashboard.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            _OAuthCallbackHandler.shutdown_flag.set()
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing auth code.")

    def log_message(self, *args) -> None:
        pass   # suppress default access log noise


# ---------------------------------------------------------------------------
# OAuth session manager
# ---------------------------------------------------------------------------

class OAuthSession:
    """
    One-shot OAuth flow for a single broker login.

    Usage:
        session = OAuthSession("UPSTOX", api_key="xxx", api_secret="yyy")
        token   = session.run()          # opens browser, blocks until done
        # token is now stored in the DB if client_id was provided
    """

    def __init__(
        self,
        broker: str,
        api_key: str,
        api_secret: str,
        client_db_id: Optional[int] = None,
        on_success: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.broker       = broker.upper()
        self.api_key      = api_key
        self.api_secret   = api_secret
        self.client_db_id = client_db_id
        self.on_success   = on_success
        self._server: Optional[HTTPServer] = None

    def run(self) -> Optional[str]:
        """
        Full OAuth flow: start server → open browser → capture code →
        exchange for token → store in DB → return token string.
        Returns None on timeout or error.
        """
        cfg = BROKER_OAUTH_CONFIG.get(self.broker)
        if cfg is None:
            logger.error("No OAuth config for broker: %s", self.broker)
            return None

        # Reset handler state
        _OAuthCallbackHandler.captured_code = None
        _OAuthCallbackHandler.shutdown_flag.clear()

        # Start callback server in background thread
        server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _OAuthCallbackHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self._server = server
        logger.info("OAuth callback server started on %s", CALLBACK_URL)

        # Build and open the broker authorization URL
        auth_url = cfg["auth_url"].format(
            api_key=self.api_key,
            redirect_uri=CALLBACK_URL,
        )
        logger.info("Opening browser for %s OAuth: %s", self.broker, auth_url)
        webbrowser.open(auth_url)

        # Wait for the callback or timeout
        got_code = _OAuthCallbackHandler.shutdown_flag.wait(timeout=OAUTH_TIMEOUT_SECS)
        server.shutdown()

        if not got_code or not _OAuthCallbackHandler.captured_code:
            logger.warning("OAuth timeout or no code received for %s", self.broker)
            return None

        code = _OAuthCallbackHandler.captured_code
        logger.info("Auth code captured for %s — exchanging for token…", self.broker)

        # Exchange code for access token
        try:
            payload = cfg["token_payload"](
                code, self.api_key, self.api_secret, CALLBACK_URL
            )
            resp = requests.post(
                cfg["token_url"],
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            resp.raise_for_status()
            data         = resp.json()
            token_field  = cfg["token_field"]
            access_token = data.get(token_field) or data.get("data", {}).get(token_field)

            if not access_token:
                logger.error(
                    "Token field '%s' not found in response: %s", token_field, data
                )
                return None

            logger.info("Access token obtained for %s", self.broker)

            # Persist to database
            if self.client_db_id is not None:
                self._save_token_to_db(self.client_db_id, access_token)

            if self.on_success:
                self.on_success(access_token)

            return access_token

        except requests.RequestException as exc:
            logger.exception("Token exchange failed for %s: %s", self.broker, exc)
            return None

    def _save_token_to_db(self, client_db_id: int, token: str) -> None:
        try:
            with db_session() as s:
                client = s.get(ClientsRegistry, client_db_id)
                if client:
                    client.access_token = token
                    logger.info(
                        "Token updated in DB for client_id=%d", client_db_id
                    )
        except Exception as exc:
            logger.exception("DB token save failed: %s", exc)


# ---------------------------------------------------------------------------
# Convenience function for use from Streamlit (runs in a thread so it
# doesn't block the UI render loop)
# ---------------------------------------------------------------------------

def start_oauth_flow_async(
    broker: str,
    api_key: str,
    api_secret: str,
    client_db_id: Optional[int] = None,
    on_success: Optional[Callable[[str], None]] = None,
) -> tuple[threading.Thread, str]:
    """
    Launch the OAuth flow in a background thread so Streamlit's render loop
    is not blocked.  The on_success callback fires when the token is ready.

    Returns:
        (thread, auth_url) — the background thread and the broker authorization
        URL that the user must open in a browser.  On headless EC2 instances
        webbrowser.open() silently fails; display auth_url via st.code() /
        st.link_button() so the user can copy it to their local browser.
    """
    broker_upper = broker.upper()
    cfg = BROKER_OAUTH_CONFIG.get(broker_upper, {})
    auth_url = cfg.get("auth_url", "").format(
        api_key=api_key,
        redirect_uri=CALLBACK_URL,
    )

    session = OAuthSession(
        broker=broker_upper,
        api_key=api_key,
        api_secret=api_secret,
        client_db_id=client_db_id,
        on_success=on_success,
    )
    t = threading.Thread(target=session.run, daemon=True, name=f"oauth-{broker_upper}")
    t.start()
    return t, auth_url
