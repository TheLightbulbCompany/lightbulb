"""Day visitor counts from the OWNER's connected PostHog (HogQL), for rollups.

Not the platform's own project (that is ``posthog_admin.py``) — this reads the
owner's PostHog via the OAuth token their MCP connect minted.

Token source: ``integration_secret_store.get(owner_id, "posthog")`` — the retained
copy, top-level ``access_token``. Query-only: this reader never refreshes and never
writes a token back.

Deletion PR-5 dropped the first source, the container's own MCP OAuth store on EFS
(``mcp_oauth._store_path``) plus the 401-refresh write-back it enabled. Hermes keeps its
rotating copy in the profile's ``mcp-tokens/`` and rotates IN PLACE, so a backend
write-back there would clobber a fresher token (the exact case
``_pull_new_mcp_token_files``' seeded-only rule exists to prevent). PostHog access
tokens live 1h, so this reader is only as good as the retained copy: it works while the
30d refresh token is alive at the vendor and a reconnect heals it after that.

Every miss (no token, HTTP error, non-200, malformed body) → ``None``, logged
once — a rollup miss is a stale tile, never a 500.

Project routing rides the token via ``/api/projects/@current/query``. If
``@current`` 404s in dev verification, the fallback is reading the project id
from the credential's ``provider_user_id`` — not implemented until observed.
"""

import logging
import re

import httpx

from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

_QUERY_URL = "https://us.posthog.com/api/projects/@current/query"
_TIMEOUT_S = 10.0
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# day is interpolated only after _DAY_RE.fullmatch — the injection fence.
_HOGQL = (
    "SELECT count(DISTINCT person_id) FROM events WHERE event = '$pageview' AND toDate(timestamp) = toDate('{day}')"
)


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
        # One guard for every failure mode past the fence: httpx transport errors, SM
        # transport errors, a malformed body. Stale tile, never a 500.
        logger.exception("posthog day_traffic failed owner=%s day=%s", owner_id, day)
        return None


async def _day_traffic(owner_id: str, day: str) -> dict | None:
    cred = await integration_secret_store.get(owner_id, "posthog")
    token = (cred or {}).get("access_token")
    if not token:
        logger.info("posthog day_traffic: no token owner=%s", owner_id)
        return None

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await _post_query(client, token, day)

    if resp.status_code != 200:
        logger.warning("posthog query non-200 owner=%s day=%s status=%s", owner_id, day, resp.status_code)
        return None
    try:
        return {"visitors": int(resp.json()["results"][0][0])}
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("posthog query malformed response owner=%s day=%s", owner_id, day)
        return None
