"""launch-entries table — one row per competing owner.

The entrant's Stripe restricted key is NOT here; it lives in Secrets Manager
on the BYO-key rail (see core.services.launch_service). Revenue numbers are
written by the hourly sweep (core.services.launch_revenue).

Post-pivot this table is ONLY the revenue-board attachment: the competition
entry itself is the sheet row (``launch_waitlist_repo``), and
``launch_service.connect_stripe`` mints this row on demand from it. A row
starts ``has_key=False`` and flips True once a validated key + baseline land.
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


async def get(owner_id: str, *, consistent: bool = False) -> dict | None:
    kwargs: dict = {"Key": {"owner_id": owner_id}}
    if consistent:
        kwargs["ConsistentRead"] = True
    resp = await run_in_thread(_table().get_item, **kwargs)
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
    idempotency contract.

    If the winning row is deleted between the failed put and the read (the
    race is real, not just theoretical — see the retry test), a bare
    ``_plain(item)`` fallback would return an item DynamoDB never actually
    holds. Instead the conditional put is retried ONCE: if it lands, the row
    now IS ``item``; if it also loses the race, some other writer's row must
    exist by now, so a second read is authoritative."""
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
        # Lost the race / already joined — the stored row is the truth. Consistent
        # read: a stale eventually-consistent GetItem right after the conditional
        # check failed can miss the winning row and send this down the retry path
        # for nothing.
        existing = await get(owner_id, consistent=True)
        if existing is not None:
            return existing
        # The winner row vanished before this read. Retry once.
        try:
            await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(owner_id)")
        except ClientError as e2:
            if e2.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing2 = await get(owner_id, consistent=True)
            if existing2 is None:
                raise RuntimeError(f"launch_entries_repo.join: row for {owner_id} vanished across two reads") from e2
            return existing2
        return _plain(item)
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


async def set_public(owner_id: str, public: bool) -> None:
    """Flip the board-visibility flag on an existing entry. ``public`` is a
    reserved DynamoDB word, hence the ``#pub`` alias (mirrors ``#hk`` above
    for ``has_key``)."""
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET #pub = :p",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeNames={"#pub": "public"},
        ExpressionAttributeValues={":p": bool(public)},
    )


async def set_display_name(owner_id: str, display_name: str) -> None:
    """Rename an existing board entry. The entry copies the sheet row's name
    at connect time, so a rename after connecting has to reach both rows or
    the leaderboard keeps rendering the old one — see
    ``launch_service.rename_display_name``, which owns that pairing."""
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET display_name = :d",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":d": display_name},
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


async def delete(owner_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"owner_id": owner_id})
