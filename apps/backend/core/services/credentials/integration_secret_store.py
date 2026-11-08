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

When ``CREDENTIAL_KMS_KEY_ARN`` is set (infra sets it in dev and prod), ``put`` writes the
``credential-envelopes`` table instead: the JSON sealed with AES-256-GCM under a fresh KMS data key
whose encryption context is ``{owner_id, provider}``, so a row copied under another owner fails to
decrypt. No new secret is created and the legacy secret is left alone, so a revert still reads it.
``get`` reads the table (consistently) and falls back to the legacy secret; ``delete`` removes both.
Legacy secrets are removed by the credential migration script, not here.
"""

from __future__ import annotations

import json
from typing import Any

import base64
import os

import boto3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.secrets_manager import create_or_update_secret


def _secret_name(owner_id: str, provider: str) -> str:
    """Secrets Manager name. Must match the resource scope granted to the
    backend task role in ``service-stack.ts`` (``lightbulb/<env>/integrations/*``),
    mirroring the ``user-keys/*`` BYOK convention."""
    env = settings.ENVIRONMENT or "dev"
    return f"lightbulb/{env}/integrations/{owner_id}/{provider}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


ENVELOPE_TABLE = "credential-envelopes"


def _kms():
    return boto3.client("kms", region_name=settings.AWS_REGION)


def _envelope_table():
    return get_table(ENVELOPE_TABLE)


def _seal(owner_id: str, provider: str, payload: str) -> dict:
    context = {"owner_id": owner_id, "provider": provider}
    key = _kms().generate_data_key(KeyId=settings.CREDENTIAL_KMS_KEY_ARN, KeySpec="AES_256", EncryptionContext=context)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key["Plaintext"]).encrypt(nonce, payload.encode(), f"{owner_id}\0{provider}".encode())
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {**context, "data_key": b64(key["CiphertextBlob"]), "nonce": b64(nonce), "ciphertext": b64(ciphertext)}


def _open(item: dict) -> dict[str, Any]:
    owner_id, provider = item["owner_id"], item["provider"]
    key = _kms().decrypt(
        CiphertextBlob=base64.b64decode(item["data_key"]),
        EncryptionContext={"owner_id": owner_id, "provider": provider},
    )["Plaintext"]
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(item["nonce"]), base64.b64decode(item["ciphertext"]), f"{owner_id}\0{provider}".encode()
    )
    return json.loads(plaintext)


def _envelopes_enabled() -> bool:
    """Whether this stage has the table and key at all (infra sets the key ARN alongside the table)."""
    return bool(settings.CREDENTIAL_KMS_KEY_ARN)


def _envelope_op(fn):
    """``fn(table)``, or None on a local run with no key ARN (the table ships with the code in dev and prod)."""
    return fn(_envelope_table()) if _envelopes_enabled() else None


async def put(owner_id: str, provider: str, credential: dict[str, Any]) -> str:
    """Create-or-update the credential. Returns where it now lives:
    ``credential-envelopes/<owner>/<provider>``, or the secret's ARN on a stage without the key."""
    name = _secret_name(owner_id, provider)
    payload = json.dumps(credential)

    def _put_envelope() -> str:
        _envelope_table().put_item(Item=_seal(owner_id, provider, payload))
        return f"{ENVELOPE_TABLE}/{owner_id}/{provider}"

    def _put_secret() -> str:
        sm = _client()
        create_or_update_secret(sm, name, payload)
        return sm.describe_secret(SecretId=name)["ARN"]

    return await run_in_thread(_put_envelope if _envelopes_enabled() else _put_secret)


async def get(owner_id: str, provider: str) -> dict[str, Any] | None:
    """Return the stored credential dict, or None when no secret exists."""
    name = _secret_name(owner_id, provider)

    def _get() -> dict[str, Any] | None:
        item = _envelope_op(
            lambda t: t.get_item(Key={"owner_id": owner_id, "provider": provider}, ConsistentRead=True).get("Item")
        )
        if item:
            return _open(item)
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
        had_envelope = bool(
            _envelope_op(
                lambda t: t.delete_item(Key={"owner_id": owner_id, "provider": provider}, ReturnValues="ALL_OLD").get(
                    "Attributes"
                )
            )
        )
        sm = _client()
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except (sm.exceptions.ResourceNotFoundException, sm.exceptions.InvalidRequestException):
            # Absent, or scheduled for deletion by the 8 Nov envelope put (already unreadable).
            return had_envelope
        return True

    return await run_in_thread(_delete)


def envelope_providers(owner_id: str) -> list[str]:
    """Every provider this owner has a sealed row for (sync; the owner wipe's sweep)."""
    from boto3.dynamodb.conditions import Key

    def _query(table) -> list[str]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "ProjectionExpression": "provider",
        }
        providers: list[str] = []
        while True:
            page = table.query(**kwargs)
            providers += [i["provider"] for i in page.get("Items", [])]
            if not page.get("LastEvaluatedKey"):
                return providers
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    return _envelope_op(_query) or []


def delete_envelope(owner_id: str, provider: str) -> None:
    _envelope_table().delete_item(Key={"owner_id": owner_id, "provider": provider})


async def github_token(owner_id: str) -> str | None:
    """The owner's stored GitHub token, or None when not connected. The paste rail stores
    ``{"token": ...}``, the direct_oauth rail stores ``access_token``."""
    credential = await get(owner_id, "github")
    if not credential:
        return None
    return credential.get("token") or credential.get("access_token")
