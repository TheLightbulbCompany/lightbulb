"""Team-entry interest rows for the Launch Competition — teams raise their
hand, the founders enter them by hand.

Stored on the **launch-settings** table (``get_table("launch-settings")``,
partition key ``id``) under an ``id = "team#{owner_id}"`` prefix. That table
is already "a small single-row-per-id config table" (database-stack.ts) and
nothing else scans it, so the prefix costs no CDK change and cannot leak into
any existing read: ``launch_settings_repo`` only ever touches ``id="settings"``.
The waitlist table would have been the wrong home — the close, the ops sheet
and the leaderboard all scan it whole, and team rows would have had to be
filtered out of every one.

One row per OWNER (``resolve_owner_id`` — the org id in an org context), so a
resubmission updates the submitter's own row instead of piling up duplicates.

``counted`` is the founders' switch: only a row they have marked counted
contributes to the pot (``core.services.launch_phase.pot_cents``). It defaults
to False and is never written by the public submission path — an unreviewed
submission must never inflate the pot.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_PREFIX = "team#"


def _table():
    return get_table("launch-settings")


def _key(owner_id: str) -> str:
    return f"{_PREFIX}{owner_id}"


def _plain(item: dict | None) -> dict | None:
    if not item:
        return None
    out = dict(item)
    out.pop("id", None)
    out["seats"] = int(out.get("seats", 0))
    out["counted"] = out.get("counted") is True
    return out


async def get(owner_id: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"id": _key(owner_id)})
    return _plain(resp.get("Item"))


async def put_interest(
    owner_id: str,
    *,
    team_name: str,
    seats: int,
    note: str,
    submitter_user_id: str,
    submitter_email: str,
    submitter_org_slug: str | None,
) -> dict:
    """Upsert the submitter's interest row — idempotent per owner.

    A resubmission replaces the details, but ``submitted_at`` and the
    founders' ``counted`` verdict survive it (``if_not_exists``).

    **The seat count is pinned once a founder has counted the row.** The
    verdict is *about a number*: if a resubmission could still move ``seats``,
    a team counted at 5 (+$35) could resubmit at 500 and silently freeze the
    pot with $2,510 nobody approved. So the write is conditional on the row
    being uncounted; when that condition loses, it is retried WITHOUT
    ``seats`` so a name or note correction still lands while the reviewed
    number stays put. Changing a counted team's seats is an admin action
    (``update_fields``), which is the whole point of the toggle.
    """
    fields = {
        "owner_id": owner_id,
        "team_name": team_name,
        "seats": int(seats),
        "note": note,
        "submitter_user_id": submitter_user_id,
        "submitter_email": submitter_email,
        "submitter_org_slug": submitter_org_slug or "",
    }
    try:
        return await _upsert(owner_id, fields, guard_uncounted=True)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Already reviewed and counted — keep the founders' number.
        return await _upsert(owner_id, {k: v for k, v in fields.items() if k != "seats"}, guard_uncounted=False)


# Only an UNCOUNTED row may have its seat count rewritten by its own submitter.
# A missing attribute is an unreviewed row (put_interest defaults it to False,
# but the very first write has no row at all).
UNCOUNTED = "attribute_not_exists(#counted) OR #counted = :false"


async def _upsert(owner_id: str, fields: dict, *, guard_uncounted: bool) -> dict:
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values: dict = {f":v{i}": v for i, v in enumerate(fields.values())}
    values[":t"] = utc_now_iso()
    values[":false"] = False
    kwargs: dict = {
        "Key": {"id": _key(owner_id)},
        "UpdateExpression": (
            "SET "
            + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names))
            + ", updated_at = :t"
            + ", submitted_at = if_not_exists(submitted_at, :t)"
            + ", #counted = if_not_exists(#counted, :false)"
        ),
        "ExpressionAttributeNames": {**names, "#counted": "counted"},
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if guard_uncounted:
        kwargs["ConditionExpression"] = UNCOUNTED
    resp = await run_in_thread(_table().update_item, **kwargs)
    return _plain(resp["Attributes"])


async def update_fields(owner_id: str, fields: dict) -> dict | None:
    """SET the given fields on an EXISTING interest row (the admin's seat
    correction / counted toggle). None when there is no such row."""
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    values[":t"] = utc_now_iso()
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"id": _key(owner_id)},
            UpdateExpression="SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)) + ", updated_at = :t",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return None
    return _plain(resp["Attributes"])


async def list_all() -> list[dict]:
    """Every interest row. Scans the launch-settings table and keeps only the
    ``team#`` ids — the settings row itself is the only other item on it."""
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        items.extend(_plain(i) for i in page.get("Items", []) if str(i.get("id", "")).startswith(_PREFIX))
        last = page.get("LastEvaluatedKey")
        if not last:
            return sorted(items, key=lambda r: r.get("submitted_at") or "")
        kwargs["ExclusiveStartKey"] = last
