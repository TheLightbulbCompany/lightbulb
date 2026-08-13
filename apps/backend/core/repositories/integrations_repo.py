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


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Delete every integration row for this owner. Returns the providers.

    Composed from list + per-provider delete rather than a scan: the table is
    partitioned by owner_id, so the query is exact and cheap. Used by the
    account wipe, which previously left these rows behind — a "wiped" owner
    kept showing connected integrations.
    """
    rows = await list_for_owner(owner_id)
    providers = [r["provider"] for r in rows if r.get("provider")]
    for provider in providers:
        await delete(owner_id, provider)
    return providers


async def get_google_watch_channels(owner_id: str) -> list[dict]:
    """The owner's live Google push channels (auto_google_watch triggers) —
    stored as a list attribute on the existing ``(owner_id, "google")`` status
    row rather than a table of their own. Each entry carries the watch's
    Google-side identity (``channel_id``, ``resource_id``, ``expiration_ms``,
    ``page_token`` for Drive), the trigger it feeds (``trigger_public_id``,
    ``proxy_url``), the account that owns it (``account_email``), and the
    ``channel_token`` (= the trigger's webhook secret — stored because a
    renewal must re-watch with the SAME token and Paperclip has no secret
    read-back; this table is CMK-encrypted and Paperclip already holds the
    identical value on the trigger row)."""
    row = await get(owner_id, "google")
    return list((row or {}).get("google_watch_channels") or [])


async def set_google_watch_channels(owner_id: str, channels: list[dict]) -> None:
    """Overwrite the ``google_watch_channels`` list on the owner's "google"
    row. UpdateExpression rather than put_item so the row's status fields
    survive (and the row is created if somehow absent).

    Callers do read-modify-write of the list. The row is per-owner, so the
    only contention is the trigger seed vs the daily renewal sweep — a lost
    update is tolerable and self-heals: an unrecorded channel simply dies at
    its ≤7-day expiry, and a resurrected stale entry is renewed and replaced
    on the next sweep."""
    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": "google"},
        UpdateExpression="SET google_watch_channels = :channels, updated_at = :now",
        ExpressionAttributeValues={":channels": channels, ":now": now},
    )


async def scan_all() -> list[dict]:
    """Every row in the table, paginated.

    Only the orphan sweep uses this — a full scan is the ONLY way to find rows
    whose owner no longer exists, because the owner id is gone from everywhere
    that would let you query for it. Not for request-path use.
    """
    table = _table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
