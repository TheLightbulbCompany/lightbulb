"""One-shot: complete the QuickBooks SANDBOX OAuth dance on localhost and write the
token store the MCP server reads.

Why this exists at all: the real flow is `oauth_dance` + the `qbo_dotenv`
materializer against api-dev.flickmeon.ai. Until that is deployed there is no way
to (a) prove the platform client actually works or (b) get a real sandbox
refresh_token + realmId to test the container end to end. DELETE THIS once a dev
owner can connect QuickBooks through the UI — git history is the record.

Requires `http://localhost:8000/callback` registered as a redirect URI on the app's
DEVELOPMENT keys. Intuit accepts localhost for sandbox and rejects it in production,
which is exactly why this is a dev-only tool.

    export QUICKBOOKS_CLIENT_ID=...      # not secret; rides every authorize URL
    export QUICKBOOKS_CLIENT_SECRET=...  # from the app's Keys page. never committed
    uv run python scripts/qbo_sandbox_connect.py
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SANDBOX_API = "https://sandbox-quickbooks.api.intuit.com"
REDIRECT_URI = "http://localhost:8000/callback"

# Accounting only. The Payments scope would ask the owner to grant money movement
# for what is a read-and-bookkeep agent; identity scopes (openid/profile/email)
# buy nothing because the connection is already owner-scoped on our side.
SCOPES = "com.intuit.quickbooks.accounting"

# Matches core/services/credentials/registry.py's quickbooks mcp_stdio block.
TOKEN_STORE = Path(os.environ.get("QUICKBOOKS_TOKEN_STORE_PATH", ".qbo-sandbox.env"))


class _Callback(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Callback.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Connected. You can close this tab.")

    def log_message(self, *_args) -> None:  # silence the default stderr spam
        pass


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(auth: str, form: dict[str, str]) -> dict:
    """Intuit's token endpoint. Errors carry `intuit_tid`, which their support asks
    for by name, so surface it rather than just the status code."""
    r = httpx.post(
        TOKEN_URL,
        headers={"Authorization": auth, "Accept": "application/json"},
        data=form,
        timeout=30,
    )
    if r.status_code != 200:
        tid = r.headers.get("intuit_tid", "<none>")
        raise SystemExit(f"token endpoint {r.status_code} (intuit_tid={tid}): {r.text}")
    return r.json()


def main() -> int:
    client_id = os.environ.get("QUICKBOOKS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("QUICKBOOKS_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("set QUICKBOOKS_CLIENT_ID and QUICKBOOKS_CLIENT_SECRET")

    state = secrets.token_urlsafe(24)
    url = (
        AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "scope": SCOPES,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "state": state,
            }
        )
    )

    server = HTTPServer(("localhost", 8000), _Callback)
    print(f"opening browser; pick a SANDBOX company\n{url}\n")
    webbrowser.open(url)
    # handle_request() blocks until ONE request is served, so no thread and no
    # polling. Loop because browsers speculatively fetch /favicon.ico, which would
    # otherwise consume the single request we are waiting for.
    while not (_Callback.result.get("code") or _Callback.result.get("error")):
        server.handle_request()
    server.server_close()
    cb = _Callback.result

    if err := cb.get("error"):
        raise SystemExit(f"authorization failed: {err}")
    # Constant-time compare: state is the CSRF guard, and a timing-leaky check on
    # the value we just minted is a pointless thing to hand an attacker.
    if not secrets.compare_digest(cb.get("state", ""), state):
        raise SystemExit("state mismatch — discarding this callback")
    code, realm_id = cb.get("code"), cb.get("realmId")
    if not code or not realm_id:
        raise SystemExit(f"callback missing code/realmId: {sorted(cb)}")

    auth = _basic_auth(client_id, client_secret)
    tok = _post_token(
        auth,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
    )

    # Prove the token actually reaches the sandbox company before writing anything.
    info = httpx.get(
        f"{SANDBOX_API}/v3/company/{realm_id}/companyinfo/{realm_id}",
        headers={"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json"},
        timeout=30,
    )
    if info.status_code != 200:
        tid = info.headers.get("intuit_tid", "<none>")
        raise SystemExit(f"companyinfo {info.status_code} (intuit_tid={tid}): {info.text}")
    name = info.json()["CompanyInfo"]["CompanyName"]

    # Exactly the keys their client reads (src/clients/quickbooks-client.ts). The
    # access token is deliberately NOT written: it dies in 60 minutes and the
    # server mints its own from the refresh token.
    TOKEN_STORE.write_text(
        "\n".join(
            [
                f"QUICKBOOKS_CLIENT_ID={client_id}",
                f"QUICKBOOKS_CLIENT_SECRET={client_secret}",
                f"QUICKBOOKS_REFRESH_TOKEN={tok['refresh_token']}",
                f"QUICKBOOKS_REALM_ID={realm_id}",
                "QUICKBOOKS_ENVIRONMENT=sandbox",
                "",
            ]
        )
    )
    TOKEN_STORE.chmod(0o600)

    print(f"connected to sandbox company: {name} (realm {realm_id})")
    print(f"token store written: {TOKEN_STORE} (0600)")
    print(f"refresh token expires in {tok['x_refresh_token_expires_in']}s (rolling)")
    print("\nthe MCP server is now the ONLY thing that may refresh this token —")
    print("running the dance again rotates it and invalidates the stored copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
