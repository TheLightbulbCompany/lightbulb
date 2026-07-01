"""Ops cache repository — DynamoDB `ops-cache` table.

A generic shared cache keyed by an opaque cache_key string, holding a JSON
`value` blob with a DynamoDB `ttl` for auto-expiry. Backs the P1 fleet
short-TTL cache and (later phases) cost/metrics snapshots.
"""

from __future__ import annotations

import time

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("ops-cache")  # -> isol8-{env}-ops-cache


async def get(cache_key: str) -> dict | None:
    table = _get_table()
    resp = await run_in_thread(table.get_item, Key={"cache_key": cache_key})
    return resp.get("Item")


async def put(cache_key: str, value: dict, ttl_seconds: int = 30) -> dict:
    table = _get_table()
    item = {
        "cache_key": cache_key,
        "value": value,
        "ttl": int(time.time()) + ttl_seconds,
        "updated_at": utc_now_iso(),
    }
    await run_in_thread(table.put_item, Item=item)
    return item
