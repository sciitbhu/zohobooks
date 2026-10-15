"""
scripts/zoho_oauth_bootstrap.py
----------------------------------
ONE-TIME setup script. Zoho's OAuth 2.0 refresh-token flow requires a
manual "grant token" step the very first time — after that, ZohoBooksClient
handles all future refreshes automatically, and you never run this again
unless you revoke access or rotate the client secret.

Steps:
  1. Register a "Server-based Application" at https://api-console.zoho.com
     - Redirect URI: http://localhost:9000/callback  (matches this script)
     - Note the Client ID and Client Secret -> put in .env
  2. Run this script:  python scripts/zoho_oauth_bootstrap.py
  3. It prints an authorization URL. Open it in your browser, log in to
     Zoho Books, and approve access.
  4. Zoho redirects to http://localhost:9000/callback?code=...
     This script runs a tiny local HTTP server to catch that redirect
     automatically and exchange the code for a refresh_token.
  5. Copy the printed refresh_token into your .env as ZOHO_REFRESH_TOKEN.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
from typing import Optional

import httpx

# --- Fill these in before running (or export as env vars and read them) ---
CLIENT_ID = "1000.CBYYS21Z9YDKRQLRRYMG2CJ0875DOI"
CLIENT_SECRET = "9fc74ce322b9aaa2fa2a2e7a247c8f51d1f365b000"
ACCOUNTS_DOMAIN = "accounts.zoho.in"  # match your region, see .env.example
REDIRECT_URI = "http://localhost:9000/callback"

# Scope kept broad for simplicity; narrow this for least-privilege in
# production once you know exactly which modules you use.
SCOPE = "ZohoBooks.fullaccess.all"

_captured_code: Optional[str] = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global _captured_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _captured_code = params.get("code", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h2>Authorization captured. You can close this tab.</h2></body></html>")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence default logging
        pass


def _wait_for_authorization_code() -> str:
    global _captured_code

    auth_url = (
        f"https://{ACCOUNTS_DOMAIN}/oauth/v2/auth"
        f"?scope={urllib.parse.quote(SCOPE)}"
        f"&client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&access_type=offline"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&prompt=consent"
    )

    print("\nOpen this URL in your browser and approve access:\n")
    print(auth_url)
    print("\nWaiting for redirect on http://localhost:9000/callback ...\n")

    webbrowser.open(auth_url)

    with socketserver.TCPServer(("localhost", 9000), _CallbackHandler) as httpd:
        thread = threading.Thread(target=httpd.handle_request)
        thread.start()
        thread.join(timeout=180)

    if not _captured_code:
        raise RuntimeError(
            "Timed out waiting for Zoho's OAuth redirect. Re-run and complete "
            "the browser approval within 3 minutes."
        )
    return _captured_code


def _exchange_code_for_tokens(code: str) -> dict:
    token_url = f"https://{ACCOUNTS_DOMAIN}/oauth/v2/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    resp = httpx.post(token_url, data=payload, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    if CLIENT_ID.startswith("your_") or CLIENT_SECRET.startswith("your_"):
        raise SystemExit(
            "Edit scripts/zoho_oauth_bootstrap.py and fill in CLIENT_ID / "
            "CLIENT_SECRET (from https://api-console.zoho.com) before running."
        )

    code = _wait_for_authorization_code()
    tokens = _exchange_code_for_tokens(code)

    if "refresh_token" not in tokens:
        raise SystemExit(f"No refresh_token in response — check scopes/redirect URI. Response: {tokens}")

    print("\n=== SUCCESS ===")
    print("Add this to your .env file:\n")
    print(f"ZOHO_REFRESH_TOKEN={tokens['refresh_token']}")
    print(
        "\nThis refresh token does not expire under normal use — "
        "ZohoBooksClient will use it to mint short-lived access tokens "
        "automatically from now on."
    )


if __name__ == "__main__":
    main()
