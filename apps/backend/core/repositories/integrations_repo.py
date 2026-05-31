"""Per-owner, per-provider integration STATUS index.

Secret-free: holds only which providers an owner has connected and the
connection status. The credential itself lives in Secrets Manager
(backend-custodian providers — see ``integration_secret_store``) or the
container EFS keyring (actor-refresh providers). Backed by the ``integrations``
table (owner_id PK, provider SK)."""

from __future__ import annotations

import time

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread


def _table():
    return get_table("integrations")  # short name; table prefix auto-applied


async def put_status(owner_id: str, provider: str, status: str = "connected") -> None:
    now = int(time.time())
    await run_in_thread(
        _table().put_item,
        Item={
            "owner_id": owner_id,
            "provider": provider,
            "status": status,
            "connected_at": now,
            "updated_at": now,
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


async def delete(owner_id: str, provider: str) -> None:
    await run_in_thread(
        _table().delete_item,
        Key={"owner_id": owner_id, "provider": provider},
    )
