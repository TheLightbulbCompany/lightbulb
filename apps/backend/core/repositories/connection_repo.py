"""Per-owner, per-provider connected-credential vault. Credential payloads are
Fernet-encrypted at rest (same key/helper as the rest of the platform). Plain
DynamoDB dicts; backed by the `connections` table (owner_id PK, provider SK)."""

from __future__ import annotations

import json
import time
from typing import Any

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table
from core.encryption import get_fernet


def _table():
    return get_table("connections")  # short name; table prefix auto-applied


def put(
    owner_id: str,
    provider: str,
    credential: dict[str, Any],
    *,
    status: str = "connected",
    meta: dict | None = None,
) -> None:
    enc = get_fernet().encrypt(json.dumps(credential).encode()).decode()
    _table().put_item(
        Item={
            "owner_id": owner_id,
            "provider": provider,
            "credential_enc": enc,
            "status": status,
            "updated_at": int(time.time()),
            **(meta or {}),
        }
    )


def get(owner_id: str, provider: str) -> dict | None:
    return _table().get_item(Key={"owner_id": owner_id, "provider": provider}).get("Item")


def list_for_owner(owner_id: str) -> list[dict]:
    return _table().query(KeyConditionExpression=Key("owner_id").eq(owner_id)).get("Items", [])


def delete(owner_id: str, provider: str) -> None:
    _table().delete_item(Key={"owner_id": owner_id, "provider": provider})


def decrypt_credential(rec: dict) -> dict[str, Any]:
    return json.loads(get_fernet().decrypt(rec["credential_enc"].encode()).decode())
