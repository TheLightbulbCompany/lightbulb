"""Short-lived PKCE-verifier + state store for the OAuth dance. Single-use,
TTL'd (DynamoDB auto-expires via the `ttl` attribute; we also check on read
since DDB TTL deletion can lag). Async, mirrors sibling repos."""

from __future__ import annotations

import time

from core.dynamodb import get_table, run_in_thread

_TTL_SECONDS = 600


def _table():
    return get_table("oauth-state")


async def put(
    state: str,
    *,
    owner_id: str,
    provider: str,
    verifier: str,
    client_id: str | None = None,
    origin: str | None = None,
    extra: dict | None = None,
) -> None:
    item: dict = {
        "state": state,
        "owner_id": owner_id,
        "provider": provider,
        "verifier": verifier,
        "ttl": int(time.time()) + _TTL_SECONDS,
        # Revoke-all compares this to its millisecond mark (started_before).
        "created_at_ms": int(time.time() * 1000),
    }
    # Bind the OAuth client this authorization was issued to into the state, so the
    # exchange uses THIS client_id rather than re-reading the app store (which a
    # concurrent connect could have overwritten with a different DCR client). Omit
    # when absent so the attribute is never written empty.
    if client_id is not None:
        item["client_id"] = client_id
    # Who minted it: "container" for agent-started flows. The callback judges
    # those against the LIVE connection at apply time (never rebind); owner-
    # started flows (no origin) keep today's behaviour. Omitted when unset.
    if origin is not None:
        item["origin"] = origin
    # Caller-defined attributes that must survive the redirect (the Slack
    # install carries agent_id + member_id). Reserved keys stay ours.
    if extra:
        for k, v in extra.items():
            if k not in item and v is not None:
                item[k] = v
    await run_in_thread(_table().put_item, Item=item)


def started_before(rec: dict, mark_ms: int) -> bool:
    """Whether the stored state was minted at or before ``mark_ms`` (revoke-all's mark). A state
    from before ``created_at_ms`` existed knows only its second (from its TTL), so it counts as
    earlier only when that whole second precedes the mark's: the boundary second is allowed."""
    if "created_at_ms" in rec:
        return int(rec["created_at_ms"]) <= mark_ms
    return int(rec["ttl"]) - _TTL_SECONDS < mark_ms // 1000


async def consume(state: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"state": state})
    rec = resp.get("Item")
    if not rec:
        return None
    await run_in_thread(_table().delete_item, Key={"state": state})  # single-use
    if int(rec["ttl"]) < int(time.time()):  # belt-and-suspenders (DDB TTL can lag)
        return None
    return rec
