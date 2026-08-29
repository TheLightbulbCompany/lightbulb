"""Per-owner OAuth-app config (client_id/secret) in AWS Secrets Manager.

Stored at ``lightbulb/<env>/integrations/<owner>/oauth-app/<provider>`` — under the
``integrations/`` prefix the backend task role has READ-WRITE on
(``service-stack.ts:453-455``). NOT ``oauth-apps/*`` (that wildcard is read-only
and a write would AccessDenied).

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

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret

logger = logging.getLogger(__name__)


def _secret_name(owner_id: str, provider: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"lightbulb/{env}/integrations/{owner_id}/oauth-app/{provider}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


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
    name = _secret_name(owner_id, provider)
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
    payload = json.dumps(record)

    def _put() -> str:
        sm = _client()
        create_or_update_secret(sm, name, payload)
        return sm.describe_secret(SecretId=name)["ARN"]

    return await run_in_thread(_put)


async def get(owner_id: str, provider: str) -> dict[str, Any] | None:
    name = _secret_name(owner_id, provider)

    def _get() -> dict[str, Any] | None:
        sm = _client()
        try:
            resp = sm.get_secret_value(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            return None
        except ClientError:
            # Secrets Manager unreachable / unauthorized / throttled: treat the
            # BYO app as not-configured rather than letting it propagate. A
            # catalog read calls this per byo-required provider on every
            # /connections GET, so an unhandled error here would 500 the whole
            # connections list. byo_configured degrades to false (the owner can
            # re-store their app), and the dance still fails closed — there is no
            # shared-app fallback to wrongly slip into.
            logger.warning("oauth_app_store.get failed for owner=%s provider=%s", owner_id, provider, exc_info=True)
            return None
        return json.loads(resp["SecretString"])

    return await run_in_thread(_get)


async def delete(owner_id: str, provider: str) -> None:
    name = _secret_name(owner_id, provider)

    def _delete() -> None:
        sm = _client()
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass

    await run_in_thread(_delete)
