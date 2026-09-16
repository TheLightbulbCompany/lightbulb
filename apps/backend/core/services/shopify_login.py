"""The Connections door for Shopify: a human-started `shopify store auth` login.

Shopify's CLI runs a PKCE grant against the owner's store with Shopify's OWN CLI
app (no app of ours, no review), but its redirect is hard-wired to
``http://127.0.0.1:13387/auth/callback`` -- loopback on whichever host runs the
CLI. So the login can only START inside the owner's container and only COMPLETE
in that container's Chrome, which the owner drives through the Browser panel.
That is why the Connect button does not redirect the owner's own browser
anywhere: this module asks the container to run the CLI (a gateway-auth route
in the baked ``lightbulb-channel`` plugin -- the gateway has no exec RPC), takes
the authorize URL the CLI printed, opens it in the container's Chrome through
the same ``browser.request`` method the Browser panel uses to start it, and then
lets the frontend poll ``login_status`` until the CLI has stored a session.

The session itself lives in the CLI's own conf file under ``XDG_CONFIG_HOME``
(EFS), refreshed by the CLI -- the backend never sees a token, so there is no
Secrets Manager entry, and the integrations row carries only the store domain as
its identity. ``logout`` drops that session on disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

import httpx

from core.services import container_rpc, runtime_transport
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

# The permanent shop host and nothing else. The same fence the retired paste rail
# used as its SSRF boundary: the match is interpolated into a URL the CONTAINER
# then fetches, and the plugin re-checks it before spawning anything.
STORE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com")
_ADMIN_URL_RE = re.compile(r"admin\.shopify\.com/store/([a-z0-9][a-z0-9-]{0,59})")
# Read-only, matching the bundled skill's request. Scope handles only; the plugin
# refuses anything else.
SCOPES = ["read_products", "read_orders", "read_customers", "read_inventory", "read_locations", "read_reports"]
PLUGIN_PREFIX = "/plugins/lightbulb-channel/shopify/"
_PLUGIN_TIMEOUT_S = 35.0  # the plugin waits up to 20 s for the CLI to print its URL
_BROWSER_TIMEOUT_S = 20.0

STORE_HINT = "Enter the store's permanent domain, like mystore.myshopify.com"


def normalize_store(raw: str) -> str:
    """The bare ``<shop>.myshopify.com`` host from whatever an owner pastes.

    Accepts the admin URL (``admin.shopify.com/store/<handle>``), a full
    ``https://<shop>.myshopify.com/...`` URL, the bare host, or just the handle.
    Anything that does not reduce to the fence raises ``ValueError`` -- a custom
    storefront domain cannot be used here, the CLI needs the permanent host.
    """
    s = (raw or "").strip().lower()
    admin = _ADMIN_URL_RE.search(s)
    if admin:
        s = f"{admin.group(1)}.myshopify.com"
    s = re.sub(r"^https?://", "", s).split("/", 1)[0].split("?", 1)[0]
    if s and "." not in s:
        s = f"{s}.myshopify.com"
    if not STORE_RE.fullmatch(s):
        raise ValueError(STORE_HINT)
    return s


def _unavailable(exc: Exception | None = None) -> ApiProblem:
    problem = ApiProblem(
        "RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3
    )
    if exc is not None:
        problem.__cause__ = exc
    return problem


async def _plugin(
    owner_id: str,
    method: str,
    sub: str,
    *,
    json_body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    container, ip = await container_rpc.running_container(owner_id)
    path = PLUGIN_PREFIX + sub + (f"?{urllib.parse.urlencode(query)}" if query else "")
    try:
        response = await runtime_transport.request(
            container,
            path,
            ip=ip,
            method=method,
            json_body=json_body,
            headers={"Authorization": f"Bearer {container['gateway_token']}"},
            timeout_s=_PLUGIN_TIMEOUT_S,
        )
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
        raise _unavailable(exc) from exc
    body = response.json()
    if not isinstance(body, dict):
        raise _unavailable()
    return body


def _error_message(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return None


async def start_login(owner_id: str, store: str) -> str:
    """Start the CLI login in the owner's container and put Shopify's authorize
    page in its Chrome. Returns the authorize URL (for logging/tests only -- the
    owner must never be handed it, it dead-ends outside the container)."""
    body = await _plugin(owner_id, "POST", "login", json_body={"store": store, "scopes": SCOPES})
    url = body.get("authorizeUrl")
    if not body.get("ok") or not isinstance(url, str) or not url:
        raise ApiProblem(
            "INTERNAL_ERROR",
            _error_message(body) or "The Shopify CLI could not start the login.",
            details={"store": store},
        )
    # Chrome first (idempotent -- the Browser panel's mount does the same), then
    # the tab. The CLI is already waiting on its callback by the time this runs.
    await container_rpc.call(
        owner_id, "browser.request", {"method": "POST", "path": "/start"}, timeout_s=_BROWSER_TIMEOUT_S
    )
    await container_rpc.call(
        owner_id,
        "browser.request",
        {"method": "POST", "path": "/tabs/open", "body": {"url": url}},
        timeout_s=_BROWSER_TIMEOUT_S,
    )
    return url


async def login_status(owner_id: str, store: str) -> dict[str, Any]:
    """``{"connected", "pending", "error", "session"}`` for the store, straight
    from the CLI's own session list (plus the plugin's memory of a login it
    started)."""
    body = await _plugin(owner_id, "GET", "status", query={"store": store})
    if not body.get("ok"):
        raise ApiProblem("INTERNAL_ERROR", _error_message(body) or "The Shopify CLI could not report its sessions.")
    return {
        "connected": bool(body.get("connected")),
        "pending": bool(body.get("pending")),
        "error": body.get("error") if isinstance(body.get("error"), str) else None,
        "session": body.get("session") if isinstance(body.get("session"), dict) else None,
    }


async def logout(owner_id: str, store: str) -> bool:
    """Drop the store's CLI session. Best-effort: a parked container cannot be
    reached without waking it, and a stale session on EFS is not a credential
    the backend holds -- the owner can always uninstall the CLI app from the
    store's Apps page, which kills it at the source."""
    try:
        body = await _plugin(owner_id, "POST", "logout", json_body={"store": store})
    except ApiProblem as exc:
        logger.warning("shopify logout skipped for owner=%s store=%s: %s", owner_id, store, exc)
        return False
    return bool(body.get("ok"))
