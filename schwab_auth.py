"""
schwab_auth.py
--------------
Handles Schwab OAuth2 authentication.
- First run: opens browser, you log in once, saves tokens
- After that: auto-refreshes tokens silently, no login needed
"""

import os
import json
import time
import base64
import requests
import webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import threading

# ─────────────────────────────────────────────
# Schwab OAuth endpoints
# ─────────────────────────────────────────────
AUTH_URL    = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL   = "https://api.schwabapi.com/v1/oauth/token"
REDIRECT    = "https://127.0.0.1"
TOKEN_FILE  = "schwab_tokens.json"

# ─────────────────────────────────────────────
# Pull credentials from environment variables
# NEVER hardcode these — set them in Railway
# ─────────────────────────────────────────────
CLIENT_ID     = os.environ.get("SCHWAB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")


def _encode_credentials():
    """Base64 encode client_id:client_secret for Basic Auth header."""
    creds = f"{CLIENT_ID}:{CLIENT_SECRET}"
    return base64.b64encode(creds.encode()).decode()


def _save_tokens(tokens: dict):
    """Save tokens to local JSON file with expiry timestamp."""
    tokens["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print("[AUTH] Tokens saved.")


def _load_tokens() -> dict:
    """Load tokens from file. Returns empty dict if not found."""
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_token_expired(tokens: dict) -> bool:
    """Check if access token is expired (with 5-min buffer)."""
    if not tokens:
        return True
    saved_at   = tokens.get("saved_at", 0)
    expires_in = tokens.get("expires_in", 1800)  # default 30 min
    expiry     = saved_at + expires_in - 300      # 5-min buffer
    return time.time() > expiry


def refresh_access_token(tokens: dict) -> dict:
    """Use refresh token to get a new access token silently."""
    print("[AUTH] Refreshing access token...")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ValueError("No refresh token found. Run first-time auth.")

    headers = {
        "Authorization": f"Basic {_encode_credentials()}",
        "Content-Type":  "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token
    }
    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    r.raise_for_status()
    new_tokens = r.json()

    # preserve refresh token if not returned in response
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = refresh_token

    _save_tokens(new_tokens)
    print("[AUTH] Access token refreshed successfully.")
    return new_tokens


# ─────────────────────────────────────────────
# First-time login flow
# Opens browser → you log in once → captures redirect
# ─────────────────────────────────────────────

_auth_code = None  # shared between server and main thread

class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server to catch Schwab's OAuth redirect."""

    def do_GET(self):
        global _auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"""
                <html><body style='font-family:sans-serif;padding:40px;'>
                <h2 style='color:green'>Authentication successful!</h2>
                <p>You can close this tab. Your trading scanner is now connected to Schwab.</p>
                </body></html>
            """)
            print("[AUTH] Authorization code captured.")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing auth code.")

    def log_message(self, format, *args):
        pass  # suppress server logs


def _run_callback_server(port=443):
    """Run a temporary local server to capture the OAuth callback."""
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.handle_request()  # handle exactly one request then stop


def first_time_login() -> dict:
    """
    Full OAuth2 first-time login flow.
    Opens browser → you log in to Schwab → tokens saved.
    Only needed ONCE — after that, refresh_access_token handles everything.
    """
    global _auth_code
    _auth_code = None

    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError(
            "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET must be set "
            "as environment variables."
        )

    # Build the authorization URL
    params = {
        "response_type": "code",
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT,
        "scope":         "readonly"
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print("\n" + "="*55)
    print("SCHWAB FIRST-TIME LOGIN")
    print("="*55)
    print("1. Your browser will open the Schwab login page.")
    print("2. Log in with your Schwab credentials.")
    print("3. Approve the access request.")
    print("4. You'll see a success message — done!")
    print("="*55 + "\n")

    # Start callback server in background thread
    server_thread = threading.Thread(
        target=_run_callback_server,
        daemon=True
    )
    server_thread.start()

    # Open browser
    time.sleep(0.5)
    webbrowser.open(auth_url)
    print("[AUTH] Browser opened. Waiting for login...")

    # Wait up to 3 minutes for user to log in
    timeout = 180
    start   = time.time()
    while _auth_code is None:
        if time.time() - start > timeout:
            raise TimeoutError("Login timed out after 3 minutes.")
        time.sleep(1)

    # Exchange auth code for tokens
    print("[AUTH] Exchanging auth code for tokens...")
    headers = {
        "Authorization": f"Basic {_encode_credentials()}",
        "Content-Type":  "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type":   "authorization_code",
        "code":         _auth_code,
        "redirect_uri": REDIRECT
    }
    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    r.raise_for_status()
    tokens = r.json()
    _save_tokens(tokens)

    print("[AUTH] Login complete. Tokens saved.")
    print("[AUTH] You won't need to log in again.\n")
    return tokens


def get_valid_tokens() -> dict:
    """
    Main entry point for getting a valid access token.
    - If no tokens exist → runs first-time login
    - If token is expired → silently refreshes
    - If token is valid → returns as-is
    """
    tokens = _load_tokens()

    if not tokens:
        print("[AUTH] No tokens found. Starting first-time login...")
        return first_time_login()

    if _is_token_expired(tokens):
        return refresh_access_token(tokens)

    return tokens


def get_access_token() -> str:
    """Convenience function — returns just the access token string."""
    tokens = get_valid_tokens()
    return tokens.get("access_token", "")
