"""Short-lived PKCE-verifier + state store for the OAuth dance. Single-use,
TTL'd (DynamoDB auto-expires via the `ttl` attribute; we also check on read
since DDB TTL deletion can lag). Async, mirrors sibling repos."""

from __future__ import annotations

import time

from core.dynamodb import get_table, run_in_thread

_TTL_SECONDS = 600


def _table():
    return get_table("oauth-state")


async def put(state: str, *, owner_id: str, provider: str, verifier: str) -> None:
    await run_in_thread(
        _table().put_item,
        Item={
            "state": state,
            "owner_id": owner_id,
            "provider": provider,
            "verifier": verifier,
            "ttl": int(time.time()) + _TTL_SECONDS,
        },
    )


async def consume(state: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"state": state})
    rec = resp.get("Item")
    if not rec:
        return None
    await run_in_thread(_table().delete_item, Key={"state": state})  # single-use
    if int(rec["ttl"]) < int(time.time()):  # belt-and-suspenders (DDB TTL can lag)
        return None
    return rec
