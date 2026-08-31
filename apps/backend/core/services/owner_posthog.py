"""Day visitor counts from the OWNER's connected PostHog (HogQL), for rollups.

Not the platform's own project (that is ``posthog_admin.py``) — this reads the
owner's PostHog via the OAuth token their MCP connect minted.

Token source, in order (PostHog access tokens live 1h; refresh tokens live 30d
and ROTATE on every use with a 120s grace window):

1. The container's EFS MCP OAuth store (``mcp_oauth._store_path``) — the copy
   the container's own SDK provider keeps fresh. The store carries no expiry
   timestamp, so we attempt the query and refresh on 401: a public-client
   (PKCE, no secret) ``grant_type=refresh_token`` POST to the registry's
   ``token_url``, then the rotated pair is written back to the SAME store file
   atomically (the 120s grace tolerates a race with the container's own
   refresh). ``invalid_grant`` → ``None`` — a 30d-idle container outlives its
   refresh token by design; owner reconnect heals it.
2. Fallback when the EFS store yields no access token:
   ``integration_secret_store.get(owner_id, "posthog")`` (the retained copy,
   top-level ``access_token``; query-only, no refresh write-back).

Every miss (no token, HTTP error, non-200, malformed body) → ``None``, logged
once — a rollup miss is a stale tile, never a 500.

Project routing rides the token via ``/api/projects/@current/query``. If
``@current`` 404s in dev verification, the fallback is reading the project id
from the credential's ``provider_user_id`` — not implemented until observed.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx

from core.services.credentials import registry
from core.services.credentials.materializers.base import atomic_write
from core.services.credentials.materializers.mcp_oauth import _store_path
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

_QUERY_URL = "https://us.posthog.com/api/projects/@current/query"
_TIMEOUT_S = 10.0
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# day is interpolated only after _DAY_RE.fullmatch — the injection fence.
_HOGQL = (
    "SELECT count(DISTINCT person_id) FROM events WHERE event = '$pageview' AND toDate(timestamp) = toDate('{day}')"
)


def _store_file(owner_id: str) -> Path:
    """The posthog MCP OAuth store file on the owner's EFS (test seam)."""
    remote = registry.get_provider("posthog").mcp_remote
    return _store_path(owner_id, remote.key, remote.url)


def _read_store(owner_id: str) -> dict | None:
    try:
        return json.loads(_store_file(owner_id).read_text())
    except (OSError, ValueError):
        return None


async def _refresh(client: httpx.AsyncClient, owner_id: str, store: dict) -> str | None:
    """Refresh the store's rotated token pair; persist both; return the new
    access token, or None (invalid_grant / malformed store)."""
    tokens = store.get("tokens") or {}
    refresh_token = tokens.get("refresh_token")
    client_id = (store.get("clientInformation") or {}).get("client_id")
    if not refresh_token or not client_id:
        return None
    token_url = registry.get_provider("posthog").oauth.token_url
    resp = await client.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    if resp.status_code != 200:
        logger.warning("posthog token refresh failed owner=%s status=%s", owner_id, resp.status_code)
        return None
    fresh = resp.json()
    tokens["access_token"] = fresh["access_token"]
    if fresh.get("refresh_token"):
        tokens["refresh_token"] = fresh["refresh_token"]
    store["tokens"] = tokens
    path = _store_file(owner_id)
    await asyncio.to_thread(atomic_write, path, json.dumps(store, indent=2).encode())
    return fresh["access_token"]


async def _post_query(client: httpx.AsyncClient, token: str, day: str) -> httpx.Response:
    return await client.post(
        _QUERY_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": {"kind": "HogQLQuery", "query": _HOGQL.format(day=day)}},
    )


async def day_traffic(owner_id: str, day: str) -> dict | None:
    """``{"visitors": int}`` — distinct $pageview persons on ``day`` (UTC), or
    ``None`` when no token is retained or the read fails. Never raises except
    the day-format ``ValueError`` fence."""
    if not _DAY_RE.fullmatch(day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    try:
        return await _day_traffic(owner_id, day)
    except Exception:
        # One guard for every failure mode past the fence: httpx transport
        # errors, atomic_write OSError on a degraded EFS mount, SM transport
        # errors, a 200 non-JSON refresh body. Stale tile, never a 500.
        logger.exception("posthog day_traffic failed owner=%s day=%s", owner_id, day)
        return None


async def _day_traffic(owner_id: str, day: str) -> dict | None:
    store = await asyncio.to_thread(_read_store, owner_id)
    token = ((store or {}).get("tokens") or {}).get("access_token")
    if not token:
        store = None  # no refresh path for the retained SM copy
        cred = await integration_secret_store.get(owner_id, "posthog")
        token = (cred or {}).get("access_token")
    if not token:
        logger.info("posthog day_traffic: no token owner=%s", owner_id)
        return None

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await _post_query(client, token, day)
        if resp.status_code == 401 and store:
            token = await _refresh(client, owner_id, store)
            if not token:
                return None
            resp = await _post_query(client, token, day)

    if resp.status_code != 200:
        logger.warning("posthog query non-200 owner=%s day=%s status=%s", owner_id, day, resp.status_code)
        return None
    try:
        return {"visitors": int(resp.json()["results"][0][0])}
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("posthog query malformed response owner=%s day=%s", owner_id, day)
        return None
