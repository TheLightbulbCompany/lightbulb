"""launch-entries table — one row per competing owner.

The entrant's Stripe restricted key is NOT here; it lives in Secrets Manager
on the BYO-key rail (see core.services.launch_service). Revenue numbers are
written by the hourly sweep (core.services.launch_revenue).

Joining (``join``) and connecting Stripe (``set_key_connected``) are two
separate writes: a row starts ``has_key=False`` at join time and flips True
only once a validated key + baseline land — see launch_service.join vs
launch_service.connect_stripe.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_INT_FIELDS = ("baseline_cents", "baseline_prorated_cents", "window_cents", "delta_cents", "waitlist_position")


def _table():
    return get_table("launch-entries")


def _plain(item: dict | None) -> dict | None:
    if not item:
        return None
    out = dict(item)
    for k in _INT_FIELDS:
        if out.get(k) is not None:
            out[k] = int(out[k])
    return out


async def get(owner_id: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id})
    return _plain(resp.get("Item"))


async def join(
    owner_id: str,
    *,
    display_name: str,
    public: bool,
    waitlist_position: int | None,
) -> dict:
    """Put-if-absent: a NEW row starts un-keyed (``has_key=False``, zeroed
    revenue fields) — ``join`` never touches Stripe. An EXISTING row is
    returned untouched (first write wins), matching the router's 200-not-409
    idempotency contract."""
    now = utc_now_iso()
    item: dict = {
        "owner_id": owner_id,
        "display_name": display_name,
        "public": bool(public),
        "waitlist_position": waitlist_position,
        "entered_at": now,
        "updated_at": now,
        "has_key": False,
        "baseline_cents": 0,
        "window_cents": 0,
        "delta_cents": 0,
    }
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(owner_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return await get(owner_id)  # lost the race / already joined — the stored row is the truth
    return _plain(item)


async def set_key_connected(owner_id: str, *, currency: str, baseline_cents: int, baseline_at: str) -> None:
    """Flips ``has_key`` True and (re)stamps the baseline. Also used by the
    refresh path to finalise a baseline that was computed over a window still
    in the future at connect time (see launch_revenue._baseline_is_provisional
    — supersedes the old ``set_baseline``)."""
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET #hk = :t, baseline_cents = :b, currency = :c, baseline_at = :a",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeNames={"#hk": "has_key"},
        ExpressionAttributeValues={":t": True, ":b": int(baseline_cents), ":c": currency, ":a": baseline_at},
    )


async def set_revenue(
    owner_id: str,
    *,
    window_cents: int,
    delta_cents: int,
    currency: str,
    baseline_prorated_cents: int | None = None,
) -> None:
    sets = ["window_cents = :w", "delta_cents = :d", "currency = :c", "refreshed_at = :t"]
    values: dict = {
        ":w": int(window_cents),
        ":d": int(delta_cents),
        ":c": currency,
        ":t": utc_now_iso(),
    }
    if baseline_prorated_cents is not None:
        sets.append("baseline_prorated_cents = :bp")
        values[":bp"] = int(baseline_prorated_cents)
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=f"SET {', '.join(sets)} REMOVE refresh_error",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues=values,
    )


async def set_refresh_error(owner_id: str, error: str) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET refresh_error = :e, refreshed_at = :t",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":e": error[:200], ":t": utc_now_iso()},
    )


async def list_all() -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        items.extend(_plain(i) for i in page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


async def count() -> int:
    # ponytail: scan-count; fine for hundreds of rows. Move to a counter row if
    # the leaderboard ever needs this on every public read at scale.
    total = 0
    kwargs: dict = {"Select": "COUNT"}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        total += int(page.get("Count", 0))
        last = page.get("LastEvaluatedKey")
        if not last:
            return total
        kwargs["ExclusiveStartKey"] = last


async def count_with_key() -> int:
    total = 0
    kwargs: dict = {
        "Select": "COUNT",
        "FilterExpression": "#hk = :t",
        "ExpressionAttributeNames": {"#hk": "has_key"},
        "ExpressionAttributeValues": {":t": True},
    }
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        total += int(page.get("Count", 0))
        last = page.get("LastEvaluatedKey")
        if not last:
            return total
        kwargs["ExclusiveStartKey"] = last


async def delete(owner_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"owner_id": owner_id})
