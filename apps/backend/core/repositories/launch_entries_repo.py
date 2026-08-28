"""launch-entries table — one row per competing owner.

The entrant's Stripe restricted key is NOT here; it lives in Secrets Manager
on the BYO-key rail (see core.services.launch_service). Revenue numbers are
written by the hourly sweep (core.services.launch_revenue).
"""

from __future__ import annotations

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


async def upsert(
    owner_id: str,
    *,
    display_name: str,
    public: bool,
    waitlist_position: int | None,
    currency: str,
    baseline_cents: int,
) -> dict:
    now = utc_now_iso()
    resp = await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=(
            "SET display_name = :n, #pub = :p, currency = :c, baseline_cents = :b, "
            "baseline_at = :now, waitlist_position = :w, updated_at = :now, "
            "entered_at = if_not_exists(entered_at, :now), "
            "window_cents = if_not_exists(window_cents, :zero), "
            "delta_cents = if_not_exists(delta_cents, :zero)"
        ),
        ExpressionAttributeNames={"#pub": "public"},
        ExpressionAttributeValues={
            ":n": display_name,
            ":p": bool(public),
            ":c": currency,
            ":b": int(baseline_cents),
            ":w": waitlist_position,
            ":now": now,
            ":zero": 0,
        },
        ReturnValues="ALL_NEW",
    )
    return _plain(resp["Attributes"])


async def set_baseline(owner_id: str, *, baseline_cents: int, currency: str, baseline_at: str) -> None:
    """Finalise a baseline that was computed over a window still in the future.

    ``baseline_at`` is the instant the numbers were read; the refresh pass
    recomputes while it predates ``competition_start_at`` (see launch_revenue)."""
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET baseline_cents = :b, currency = :c, baseline_at = :a",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":b": int(baseline_cents), ":c": currency, ":a": baseline_at},
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
