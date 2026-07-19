"""Server-side refresh of the Instagram long-lived token (refresh_owner:
isol8_rematerialize).

Instagram's ~60-day tokens are refreshed with the TOKEN ALONE
(``refresh_access_token`` — no client secret), so this can run anywhere in the
backend without touching the platform app secret. The refreshed credential is
re-put to Secrets Manager and re-materialized onto EFS so the agent's
``credentials.json`` never goes stale.

Called lazily from ``GET /connections`` (fire-and-forget) whenever the owner
opens Settings and the token is inside the refresh window. Tokens must be at
least 24h old to refresh — Instagram rejects earlier attempts — which the
30-days-out window guarantees by construction. A scheduled sweep for owners who
never open Settings ships with the ads PR; until then a token dies only after
60 days of the owner never visiting Settings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from core.services.credentials import apply_connection, connection_lock, integration_secret_store

logger = logging.getLogger(__name__)

_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
# Refresh whenever less than this much of the 60-day lifetime remains. Wide on
# purpose: every Settings visit inside the window renews the full 60 days.
_REFRESH_WINDOW = timedelta(days=30)


def _is_stale(token_expiry: str) -> bool:
    if not token_expiry:
        # Legacy/odd credential with no recorded expiry: attempt refresh; the
        # worst case is Instagram declining (<24h-old token) and we log-and-move-on.
        return True
    try:
        expiry = datetime.fromisoformat(token_expiry)
    except ValueError:
        return True
    return expiry - datetime.now(timezone.utc) < _REFRESH_WINDOW


async def refresh_instagram_if_stale(owner_id: str) -> bool:
    """Refresh + re-materialize the owner's Instagram token when it's inside the
    refresh window. Returns True only when a refresh actually happened. Failures
    are logged, never raised — this runs fire-and-forget behind a Settings read,
    and a revoked grant is surfaced to the agent/owner through API 401s, not here."""
    credential = await integration_secret_store.get(owner_id, "instagram")
    if not credential or not credential.get("access_token"):
        return False
    if not _is_stale(credential.get("token_expiry", "")):
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                _REFRESH_URL,
                params={"grant_type": "ig_refresh_token", "access_token": credential["access_token"]},
            )
        resp.raise_for_status()
        tok = resp.json()
        expires_in = int(tok.get("expires_in") or 0)
        credential["access_token"] = tok["access_token"]
        credential["expires_in"] = expires_in or None
        credential["token_expiry"] = (
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat() if expires_in else ""
        )
        # This task started fire-and-forget from GET /connections and has been
        # awaiting Meta for seconds. A concurrent DELETE /connections/instagram
        # tears down the SM secret + EFS artifacts + status row; if it interleaves
        # with the writes below, we'd resurrect a live token + re-enable the skill
        # behind a UI that reads disconnected. Take the per-owner connection lock
        # (disconnect takes the same one) so the recheck→write section is atomic
        # w.r.t. disconnect, then re-read the secret under the lock: gone => the
        # owner disconnected first, so abort without writing. The lock is taken
        # AFTER the slow Meta call, never around it.
        async with connection_lock.connection_lock(owner_id, "instagram"):
            if await integration_secret_store.get(owner_id, "instagram") is None:
                logger.info("instagram refresh aborted for %s: disconnected mid-refresh", owner_id)
                return False
            # Rematerialize EFS (the token the agent actually reads) BEFORE advancing
            # the retained credential's expiry. If apply fails here, the stored expiry
            # stays in the past, so the next GET /connections retries — otherwise
            # Secrets Manager would read fresh for ~60d while credentials.json kept the
            # dead token, and the agent would 401 with no self-heal.
            await apply_connection.apply_connection(owner_id, "instagram", credential)
            await integration_secret_store.put(owner_id, "instagram", credential)
        logger.info("instagram token refreshed for %s", owner_id)
        return True
    except Exception:
        logger.warning("instagram token refresh failed for %s", owner_id, exc_info=True)
        return False
