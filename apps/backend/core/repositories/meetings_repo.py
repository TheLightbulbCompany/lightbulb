"""Meeting rows: one containers-table row per calendar invite, keyed by the invite's UID.

``status`` puts the row on the containers table's sparse ``status-index``, so ``list_due``
is one query on ``scheduled`` -- no container row ever carries that status.
"""

from __future__ import annotations

from core.dynamodb import get_table, run_in_thread
from core.repositories import container_repo

MEETING_KEY_PREFIX = "__meeting__#"


def _table():
    return get_table("containers")


def _key(owner_id: str, uid: str) -> dict:
    return {"owner_id": f"{MEETING_KEY_PREFIX}{owner_id}#{uid}"}


async def put_meeting(row: dict) -> None:
    await run_in_thread(_table().put_item, Item={**row, **_key(row["ownerId"], row["uid"])})


async def get_meeting(owner_id: str, uid: str) -> dict | None:
    return (await run_in_thread(_table().get_item, Key=_key(owner_id, uid), ConsistentRead=True)).get("Item")


async def list_due(now: int, within_s: int) -> list[dict]:
    """Scheduled meetings starting in ``[now, now + within_s]`` (epoch seconds)."""
    return [
        r
        for r in await container_repo.get_by_status("scheduled")
        if str(r.get("owner_id", "")).startswith(MEETING_KEY_PREFIX) and now <= int(r["startAt"]) <= now + within_s
    ]


async def set_status(owner_id: str, uid: str, status: str, **fields) -> None:
    """Set ``status`` (and any extra fields) on an existing meeting row."""
    values = {"status": status, **fields}
    await run_in_thread(
        _table().update_item,
        Key=_key(owner_id, uid),
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
        ExpressionAttributeNames={f"#{k}": k for k in values},
        ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
        ConditionExpression="attribute_exists(owner_id)",
    )
