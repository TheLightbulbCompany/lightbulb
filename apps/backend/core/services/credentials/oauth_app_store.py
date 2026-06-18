"""Per-owner BYO OAuth-app config (client_id/secret) in AWS Secrets Manager.

Stored at ``isol8/<env>/integrations/<owner>/oauth-app/<provider>`` — under the
``integrations/`` prefix the backend task role has READ-WRITE on
(``service-stack.ts:453-455``). NOT ``oauth-apps/*`` (that wildcard is read-only
and a write would AccessDenied).

This holds ONLY the app config, never a token — for google the token lives in
the in-container gog keyring (``refresh_owner: actor``); ``_oauth_client``
re-reads this app config on every connect/reconnect.
"""

from __future__ import annotations

import json
from typing import Any

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret


def _secret_name(owner_id: str, provider: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/integrations/{owner_id}/oauth-app/{provider}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


async def put(owner_id: str, provider: str, *, client_id: str, client_secret: str, acknowledged_at: str) -> str:
    name = _secret_name(owner_id, provider)
    payload = json.dumps({"client_id": client_id, "client_secret": client_secret, "acknowledged_at": acknowledged_at})

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
