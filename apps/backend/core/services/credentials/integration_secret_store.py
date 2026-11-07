"""Per-owner, per-provider integration credential store backed by AWS Secrets
Manager.

Replaces the DynamoDB credential vault. A retained integration credential — a
paste-key token, or any OAuth credential whose custodian is the backend rather
than a self-refreshing container CLI — lives at
``lightbulb/<env>/integrations/<owner>/<provider>`` as a JSON blob, encrypted at rest
with the AWS-managed Secrets Manager key (same convention as the ``user-keys``
BYOK secrets — no explicit ``KmsKeyId``). No credential ciphertext is stored in
DynamoDB.

The payload is the exact ``credential`` dict that ``apply_connection`` consumes,
so a later re-inject (marketplace slot-fill, reconnect) round-trips it unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret


def _secret_name(owner_id: str, provider: str) -> str:
    """Secrets Manager name. Must match the resource scope granted to the
    backend task role in ``service-stack.ts`` (``lightbulb/<env>/integrations/*``),
    mirroring the ``user-keys/*`` BYOK convention."""
    env = settings.ENVIRONMENT or "dev"
    return f"lightbulb/{env}/integrations/{owner_id}/{provider}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


async def put(owner_id: str, provider: str, credential: dict[str, Any]) -> str:
    """Create-or-update the secret with the credential dict. Returns the ARN."""
    name = _secret_name(owner_id, provider)
    payload = json.dumps(credential)

    def _put() -> str:
        sm = _client()
        create_or_update_secret(sm, name, payload)
        return sm.describe_secret(SecretId=name)["ARN"]

    return await run_in_thread(_put)


async def get(owner_id: str, provider: str) -> dict[str, Any] | None:
    """Return the stored credential dict, or None when no secret exists."""
    name = _secret_name(owner_id, provider)

    def _get() -> dict[str, Any] | None:
        sm = _client()
        try:
            resp = sm.get_secret_value(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            return None
        except sm.exceptions.InvalidRequestException:
            # The secret exists but is SCHEDULED FOR DELETION (the recovery
            # window after a disconnect that did NOT force-delete, or a manual
            # console deletion). GetSecretValue refuses to read it. Treat it as
            # "not connected" — the same as absent — so the connections UI can
            # re-stage a fresh connect instead of 500ing for the whole 7-30 day
            # window. ``delete()`` force-deletes, so new disconnects never land
            # here; this covers legacy/manually-scheduled deletions.
            return None
        return json.loads(resp["SecretString"])

    return await run_in_thread(_get)


async def delete(owner_id: str, provider: str) -> bool:
    """Force-delete the secret (no recovery window) so a disconnect stops the
    credential from being re-injected immediately. No-op when the secret is
    absent — actor-refresh providers never stored one. Returns whether a secret
    was actually deleted."""
    name = _secret_name(owner_id, provider)

    def _delete() -> bool:
        sm = _client()
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            return False
        return True

    return await run_in_thread(_delete)


async def github_token(owner_id: str) -> str | None:
    """The owner's stored GitHub token, or None when not connected. The paste rail stores
    ``{"token": ...}``, the direct_oauth rail stores ``access_token``."""
    credential = await get(owner_id, "github")
    if not credential:
        return None
    return credential.get("token") or credential.get("access_token")
