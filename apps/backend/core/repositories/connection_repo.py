"""Per-owner, per-provider connected-credential vault. Credential payloads are
Fernet-encrypted at rest (same key/helper as the rest of the platform). Plain
DynamoDB dicts; backed by the `connections` table (owner_id PK, provider SK)."""

from __future__ import annotations

import json
import time
from typing import Any

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread
from core.encryption import get_fernet


def _table():
    return get_table("connections")  # short name; table prefix auto-applied


async def put(
    owner_id: str,
    provider: str,
    credential: dict[str, Any],
    *,
    status: str = "connected",
    meta: dict | None = None,
) -> None:
    enc = get_fernet().encrypt(json.dumps(credential).encode()).decode()
    await run_in_thread(
        _table().put_item,
        Item={
            **(meta or {}),
            "owner_id": owner_id,
            "provider": provider,
            "credential_enc": enc,
            "status": status,
            "updated_at": int(time.time()),
        },
    )


async def get(owner_id: str, provider: str) -> dict | None:
    response = await run_in_thread(
        _table().get_item,
        Key={"owner_id": owner_id, "provider": provider},
    )
    return response.get("Item")


async def list_for_owner(owner_id: str) -> list[dict]:
    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])


async def scrub_secret(owner_id: str, provider: str) -> None:
    """Drop the encrypted secret from a connection row while keeping the record.

    For actor-refresh providers the container self-refreshes its own token and
    becomes the sole durable custodian of the credential, so the vault has no
    reason to retain the secret once it has been delivered. Removing the
    ciphertext (not the row) preserves owner/provider/status/meta — list still
    reports the integration as connected and disconnect still resolves — while
    leaving the backend holding nothing. `secret_retained=False` flags that the
    absence of `credential_enc` is intentional non-retention, not corruption.

    Conditional on the row existing so a lost race with disconnect cannot
    resurrect a deleted record."""
    table = _table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "provider": provider},
            UpdateExpression="REMOVE credential_enc SET secret_retained = :f, updated_at = :t",
            ExpressionAttributeValues={":f": False, ":t": int(time.time())},
            ConditionExpression="attribute_exists(owner_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Row already gone (e.g. concurrent disconnect) — nothing to scrub.
        return


async def delete(owner_id: str, provider: str) -> None:
    await run_in_thread(
        _table().delete_item,
        Key={"owner_id": owner_id, "provider": provider},
    )


def decrypt_credential(rec: dict) -> dict[str, Any]:
    enc = rec.get("credential_enc")
    if not enc:
        raise ValueError("connection record has no credential_enc")
    return json.loads(get_fernet().decrypt(enc.encode()).decode())
