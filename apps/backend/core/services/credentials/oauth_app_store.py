"""Per-owner OAuth-app config (client_id/secret), sealed in ``integration_secret_store``
under provider ``oauth-app/<provider>``. Its legacy Secrets Manager name,
``lightbulb/<env>/integrations/<owner>/oauth-app/<provider>``, is exactly the store's fallback read,
so an unmigrated app config still reads; the next ``put`` (or the one-time migration) retires it.

This holds ONLY the app config, never a token — for google the token lives in
the in-container gog keyring (``refresh_owner: actor``); ``_oauth_client``
re-reads this app config on every connect/reconnect.

Two writers:
- the BYO flow stores the owner's own ``client_id`` + ``client_secret``.
- the Dynamic Client Registration flow stores a DCR-registered PUBLIC client
  (``client_secret`` empty) plus the full registration response under
  ``client_information`` so the mcp_oauth materializer can write it verbatim into
  the MCP SDK's OAuth store.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


def _provider(provider: str) -> str:
    return f"oauth-app/{provider}"


async def put(
    owner_id: str,
    provider: str,
    *,
    client_id: str,
    client_secret: str,
    client_information: dict[str, Any] | None = None,
    scope: str | None = None,
    redirect_uri: str | None = None,
) -> str:
    record: dict[str, Any] = {"client_id": client_id, "client_secret": client_secret}
    # Only DCR clients carry the full registration response; a BYO app stores just
    # the id/secret pair, so omit the key entirely when absent.
    if client_information is not None:
        record["client_information"] = client_information
    # The scope list a DCR client was registered under. start() re-registers (and
    # overwrites this record) when the current requested scope differs, so a scope
    # change propagates without deleting + recreating the secret — which would race
    # AWS's async secret-name release on a quick reconnect.
    if scope is not None:
        record["scope"] = scope
    # The redirect URI a DCR client was registered under. DCR PINS
    # ``redirect_uris`` at registration, so a callback-URL change (a domain
    # move) leaves every existing client rejecting the new URI with nothing to
    # detect the drift — start() compares this to re-register instead.
    if redirect_uri is not None:
        record["redirect_uri"] = redirect_uri
    return await integration_secret_store.put(owner_id, _provider(provider), record)


async def get(owner_id: str, provider: str) -> dict[str, Any] | None:
    try:
        return await integration_secret_store.get(owner_id, _provider(provider))
    except ClientError:
        # AWS unreachable / unauthorized / throttled: treat the BYO app as not-configured
        # rather than letting it propagate. A catalog read calls this per byo-required
        # provider on every /connections GET, so an unhandled error here would 500 the whole
        # connections list. byo_configured degrades to false (the owner can re-store their
        # app), and the dance still fails closed — there is no shared-app fallback.
        logger.warning("oauth_app_store.get failed for owner=%s provider=%s", owner_id, provider, exc_info=True)
        return None


async def delete(owner_id: str, provider: str) -> None:
    await integration_secret_store.delete(owner_id, _provider(provider))
