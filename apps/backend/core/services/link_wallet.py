"""Stripe Link Agent Wallet: the backend's whole conversation with Link.

Server-side OAuth (authorization code + PKCE S256 + ``state``) against ``login.link.com`` and the
spend-request API on ``api.link.com`` -- the REST shape the open-source ``stripe/link-cli`` SDKs
use. No Node CLI, no agent-reachable token.

Custody: ONE Secrets Manager blob per workspace (``integration_secret_store``, provider
``link_wallet``; the owner-prefix sweep takes it on an account wipe) holding the access token,
the ROTATING refresh token, the expiry and who connected. Refresh tokens rotate on every use, so
a refresh runs under the owner-lifecycle lease and re-reads the blob first: of two racing
refreshes the second finds the first's fresh token and spends nothing.

What never leaves this module: a token, or a payment credential. ``spend_request`` returns a
whitelist of fields, so even if Link echoed a ``card`` object on create, it is dropped here.
(Phase 1 never retrieves a credential at all; delivering one to a checkout is a follow-up.)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from core.config import settings
from core.repositories import oauth_state_repo
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import owner_lifecycle
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "link_wallet"  # integration_secret_store key; also the oauth-state provider
SCOPE = "payment_methods.agentic userinfo:read"
REQUIRED_SCOPE = "payment_methods.agentic"
LOGIN_BASE = "https://login.link.com"
API_BASE = "https://api.link.com"
REFRESH_AHEAD_S = 120
TIMEOUT_S = 15

# Tests swap in an httpx.MockTransport; production leaves it None.
_transport: httpx.AsyncBaseTransport | None = None


class LinkWalletError(Exception):
    """Safe to show: never carries a token or a credential. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def redirect_uri() -> str:
    return f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/company/link-wallet/callback"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=TIMEOUT_S)


def _configured() -> None:
    if not configured():
        raise LinkWalletError("not_configured", "The Link wallet isn't configured for this environment.")


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 URL-safe chars (spec: 43-128)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def start_connect(owner_id: str, user_id: str) -> str:
    """The one-shot authorize URL. The verifier and state stay server-side (oauth-state, 10 min,
    single use); the URL carries only the challenge."""
    _configured()
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce()
    await oauth_state_repo.put(
        state, owner_id=owner_id, provider=PROVIDER, verifier=verifier, extra={"user_id": user_id}
    )
    query = urlencode(
        {
            "key": settings.LINK_PUBLISHABLE_KEY,
            "client_id": settings.LINK_CLIENT_ID,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{LOGIN_BASE}/auth?{query}"


async def _token_request(form: dict[str, str]) -> dict[str, Any]:
    _configured()
    body = {"client_id": settings.LINK_CLIENT_ID, "client_secret": settings.LINK_CLIENT_SECRET, **form}
    async with _client() as http:
        res = await http.post(
            f"{LOGIN_BASE}/auth/token",
            data=body,
            headers={"Authorization": f"Bearer {settings.LINK_PUBLISHABLE_KEY}"},
        )
    if res.status_code in (400, 401):
        # invalid_grant: the grant was revoked, or the refresh token already rotated away.
        raise LinkWalletError("reauthorize", "Link needs to be connected again.")
    if res.status_code >= 300:
        logger.warning("link_wallet: token endpoint answered %s", res.status_code)
        raise LinkWalletError("link_unavailable", "Link isn't answering. Try again in a moment.")
    return res.json()


def _credential(token: dict[str, Any], *, connected_by: str) -> dict[str, Any]:
    return {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": int(time.time()) + int(token.get("expires_in") or 3600),
        "scope": token.get("scope") or "",
        "connected_by": connected_by,
        "connected_at": int(time.time()),
    }


async def exchange_code(rec: dict, code: str) -> bool:
    """Land the callback: ``rec`` is the consumed oauth-state row. False = refused (revoked
    while connecting, or Link granted less than we need); True = connected."""
    token = await _token_request(
        {
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
            "code": code,
            "code_verifier": rec["verifier"],
        }
    )
    owner_id = rec["owner_id"]
    # A customer can approve a subset: read what was granted, not what was asked.
    if REQUIRED_SCOPE not in str(token.get("scope") or "").replace(",", " ").split():
        await _revoke(token.get("refresh_token"))
        return False
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        if await owner_lifecycle.revoked_since(owner_id, rec):
            await _revoke(token.get("refresh_token"))
            return False
        # A second connect replaces the first: hand the old grant back rather than orphan it.
        old = await integration_secret_store.get(owner_id, PROVIDER)
        await integration_secret_store.put(owner_id, PROVIDER, _credential(token, connected_by=rec.get("user_id", "")))
    if old:
        await _revoke(old.get("refresh_token"))
    return True


async def _revoke(refresh_token: str | None) -> None:
    """Best-effort: ending the grant at Link. A failure is logged without the token."""
    if not refresh_token or not (settings.LINK_CLIENT_ID and settings.LINK_PUBLISHABLE_KEY):
        return
    try:
        async with _client() as http:
            await http.post(
                f"{LOGIN_BASE}/auth/revoke",
                data={
                    "client_id": settings.LINK_CLIENT_ID,
                    "client_secret": settings.LINK_CLIENT_SECRET,
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                },
                headers={"Authorization": f"Bearer {settings.LINK_PUBLISHABLE_KEY}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("link_wallet: revoke failed (%s)", type(exc).__name__)


async def disconnect(owner_id: str) -> bool:
    """Revoke at Link, then delete our copy. The copy goes even if Link is unreachable."""
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        cred = await integration_secret_store.get(owner_id, PROVIDER)
        if not cred:
            return False
        await _revoke(cred.get("refresh_token"))
        await integration_secret_store.delete(owner_id, PROVIDER)
    return True


# ---------------------------------------------------------------------------
# connection state
# ---------------------------------------------------------------------------


async def _connector_is_member(owner_id: str, user_id: str) -> bool:
    workspace = await WorkspaceRepo().get_workspace(owner_id)
    return bool(user_id) and user_id in ((workspace.member_user_ids if workspace else None) or [])


async def connection(owner_id: str) -> dict | None:
    """The stored credential, or None when disconnected. A wallet is the connecting admin's own
    Link account: when they leave the workspace it reads disconnected and the grant is ended."""
    cred = await integration_secret_store.get(owner_id, PROVIDER)
    if not cred:
        return None
    if not await _connector_is_member(owner_id, cred.get("connected_by", "")):
        logger.info("link_wallet: connector left owner_id=%s; disconnecting", owner_id)
        await disconnect(owner_id)
        return None
    return cred


def configured() -> bool:
    """Link's OAuth client and the pk its /auth calls need. Unconfigured, every route 404s."""
    return bool(settings.LINK_CLIENT_ID and settings.LINK_CLIENT_SECRET and settings.LINK_PUBLISHABLE_KEY)


async def status(owner_id: str) -> dict:
    cred = await connection(owner_id)
    return {
        "connected": cred is not None,
        "connectedBy": (cred or {}).get("connected_by"),
        "scope": (cred or {}).get("scope"),
        "configured": configured(),
    }


async def _access_token(owner_id: str) -> str:
    cred = await connection(owner_id)
    if not cred:
        raise LinkWalletError("wallet_disconnected", "The Link wallet isn't connected.")
    if cred["expires_at"] - time.time() > REFRESH_AHEAD_S:
        return cred["access_token"]
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        cred = await integration_secret_store.get(owner_id, PROVIDER)  # a racing refresh may have rotated it
        if not cred:
            raise LinkWalletError("wallet_disconnected", "The Link wallet isn't connected.")
        if cred["expires_at"] - time.time() > REFRESH_AHEAD_S:
            return cred["access_token"]
        token = await _token_request({"grant_type": "refresh_token", "refresh_token": cred["refresh_token"]})
        fresh = _credential(
            {**token, "refresh_token": token.get("refresh_token") or cred["refresh_token"]},
            connected_by=cred["connected_by"],
        )
        fresh["connected_at"] = cred.get("connected_at", fresh["connected_at"])
        # The old refresh token is dead now: persist the new one before anything else can fail.
        await integration_secret_store.put(owner_id, PROVIDER, fresh)
        return fresh["access_token"]


# ---------------------------------------------------------------------------
# spend requests
# ---------------------------------------------------------------------------

_SAFE = ("id", "status", "amount", "currency", "merchant_name", "merchant_url", "approval_url", "expires_at")


async def _api(owner_id: str, method: str, path: str, json: dict | None = None) -> dict[str, Any]:
    token = await _access_token(owner_id)
    async with _client() as http:
        res = await http.request(method, f"{API_BASE}{path}", json=json, headers={"Authorization": f"Bearer {token}"})
    if res.status_code == 401:
        raise LinkWalletError("reauthorize", "Link needs to be connected again.")
    if res.status_code >= 300:
        logger.warning("link_wallet: %s %s answered %s", method, path.split("?")[0], res.status_code)
        raise LinkWalletError("link_rejected", "Link refused the request.")
    data = res.json() if res.content else {}
    # Whitelist: a card, token or address that Link echoes is dropped here, never passed on.
    return {k: data[k] for k in _SAFE if k in data}


async def create_spend_request(
    owner_id: str,
    *,
    request_id: str,
    amount_cents: int,
    merchant_name: str,
    merchant_url: str,
    context: str,
    metadata: dict[str, str],
) -> dict[str, Any]:
    """``request_id`` is our ledger id and Link's idempotency key: a retry returns the original."""
    return await _api(
        owner_id,
        "POST",
        "/spend_requests",
        {
            "idempotency_key": request_id,
            "amount": amount_cents,
            "currency": "usd",
            "merchant_name": merchant_name,
            "merchant_url": merchant_url,
            "context": context,
            "metadata": metadata,
            "request_approval": True,
        },
    )


async def retrieve_spend_request(owner_id: str, link_id: str) -> dict[str, Any]:
    """Status only: no ``include`` parameter, so Link sends no credential."""
    return await _api(owner_id, "GET", f"/spend_requests/{link_id}")


async def cancel_spend_request(owner_id: str, link_id: str) -> dict[str, Any]:
    """``POST /spend_requests/{id}/cancel`` (stripe/link-cli sdk-go): ends an unused request."""
    return await _api(owner_id, "POST", f"/spend_requests/{link_id}/cancel")
