"""``lightbulb login``: which Lightbulb user each computer (its network member key) belongs to.

One row per key, written only by ``network_login`` after the key signed a backend challenge and
the signed-in person confirmed. A ``by-user`` GSI answers the reverse question: the keys of a
workspace's members' computers (``network_member``'s ``member_keys``).
"""

from __future__ import annotations

import asyncio

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_TABLE = "network-keys"


def _table():
    return get_table(_TABLE)


async def get(key: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"key": key})
    return resp.get("Item")


async def put(key: str, user_id: str, computer: str) -> dict | None:
    """Bind ``key`` to ``user_id`` (a re-login by someone else replaces the binding). Returns
    the row it replaced, if any, so the caller can update the previous user's workspaces."""
    resp = await run_in_thread(
        _table().put_item,
        Item={"key": key, "user_id": user_id, "computer": computer, "created_at": utc_now_iso()},
        ReturnValues="ALL_OLD",
    )
    return resp.get("Attributes") or None


async def delete(key: str) -> dict | None:
    resp = await run_in_thread(_table().delete_item, Key={"key": key}, ReturnValues="ALL_OLD")
    return resp.get("Attributes") or None


async def _keys_of(user_id: str) -> list[str]:
    keys: list[str] = []
    kwargs = {"IndexName": "by-user", "KeyConditionExpression": Key("user_id").eq(user_id)}
    while True:
        resp = await run_in_thread(_table().query, **kwargs)
        keys += [row["key"] for row in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            return keys
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def _consistent(keys: set[str]) -> dict[str, dict]:
    """The base-table rows of ``keys``, strongly consistent (the GSI may lag a put or delete)."""
    table = _table()
    out: dict[str, dict] = {}
    pending = [{"key": k} for k in sorted(keys)]
    while pending:
        batch, pending = pending[:100], pending[100:]
        request = {table.name: {"Keys": batch, "ConsistentRead": True}}
        while request:
            resp = await run_in_thread(table.meta.client.batch_get_item, RequestItems=request)
            for row in resp.get("Responses", {}).get(table.name, []):
                out[row["key"]] = row
            request = resp.get("UnprocessedKeys") or None
    return out


async def keys_for_users(user_ids: list[str], extra: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> list[str]:
    """Every key bound to one of ``user_ids``, sorted. The ``by-user`` GSI names the candidates
    (plus ``extra``: a key just bound, which the GSI may not show yet); each is then checked
    against a consistent read of its row, so a key just deleted or moved to someone else is
    never listed. ``exclude`` drops keys about to be unbound."""
    users = set(user_ids)
    found = await asyncio.gather(*(_keys_of(u) for u in users))
    candidates = ({k for keys in found for k in keys} | set(extra)) - set(exclude)
    rows = await _consistent(candidates) if candidates else {}
    return sorted(k for k, row in rows.items() if row.get("user_id") in users)
