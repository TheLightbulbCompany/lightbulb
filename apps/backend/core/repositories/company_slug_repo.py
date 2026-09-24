"""Platform-unique company slugs.

One row per reserved slug, with the slug as the partition key — which makes
reservation a *conditional put*, the only atomic "claim this name" primitive
DynamoDB offers. Read-then-write would let two owners race onto the same
slug. A ``by-owner`` GSI answers the reverse question.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

import time

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("company-slugs")


def normalize(value: str) -> str:
    return value.strip().lower()


async def reserve(*, slug: str, owner_id: str) -> bool:
    """Claim ``slug`` for ``owner_id``.

    Returns True when the slug is now held by this owner — including the
    case where they already held it, since provisioning is idempotent and
    may re-run. Returns False when another owner holds it.
    """
    table = _get_table()
    item = {
        "slug": normalize(slug),
        "owner_id": owner_id,
        "created_at": utc_now_iso(),
    }
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(slug) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


#: A slug chosen at "Create your workspace" is held for the USER before the Clerk org exists
#: (the org is only created once the hold is won), then handed to the org by the
#: ``organization.created`` webhook. An abandoned create frees its slug after ``HOLD_TTL_S``.
PENDING_PREFIX = "pending:"
HOLD_TTL_S = 30 * 60


def pending_owner(user_id: str) -> str:
    return f"{PENDING_PREFIX}{user_id}"


def is_stale_hold(row: dict) -> bool:
    return str(row.get("owner_id", "")).startswith(PENDING_PREFIX) and int(row.get("held_at", 0)) < (
        time.time() - HOLD_TTL_S
    )


async def hold(*, slug: str, user_id: str) -> bool:
    """Hold ``slug`` for ``user_id`` ahead of workspace creation. True when this user now
    holds it (free, already theirs, or a stale hold); False when someone else holds it."""
    owner = pending_owner(user_id)
    try:
        await run_in_thread(
            _get_table().put_item,
            Item={"slug": normalize(slug), "owner_id": owner, "created_at": utc_now_iso(), "held_at": int(time.time())},
            ConditionExpression=(
                "attribute_not_exists(slug) OR owner_id = :owner"
                " OR (begins_with(owner_id, :pending) AND held_at < :cutoff)"
            ),
            ExpressionAttributeValues={
                ":owner": owner,
                ":pending": PENDING_PREFIX,
                ":cutoff": int(time.time() - HOLD_TTL_S),
            },
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def claim_hold(*, user_id: str, owner_id: str) -> str | None:
    """Hand ``user_id``'s newest hold to the workspace ``owner_id``. Returns the slug, or
    None when the user holds nothing (or lost it). Idempotent: a replay after the hand-over
    finds no pending row and changes nothing."""
    table = _get_table()
    pending = pending_owner(user_id)
    response = await run_in_thread(
        table.query, IndexName="by-owner", KeyConditionExpression=Key("owner_id").eq(pending)
    )
    rows = sorted(response.get("Items", []), key=lambda r: int(r.get("held_at", 0)), reverse=True)
    if not rows:
        return None
    slug = rows[0]["slug"]
    try:
        await run_in_thread(
            table.update_item,
            Key={"slug": slug},
            UpdateExpression="SET owner_id = :owner REMOVE held_at",
            ConditionExpression="owner_id = :pending",
            ExpressionAttributeValues={":owner": owner_id, ":pending": pending},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return slug


async def get_by_slug(slug: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"slug": normalize(slug)})
    return response.get("Item")


async def get_by_owner(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


async def delete_for_owner(owner_id: str) -> str | None:
    """Release this owner's slug reservation. Returns the slug, if any.

    Without this a wiped account held its slug forever: there was no delete
    path, so the name could never be handed to another owner — and the same
    person re-signing up could not reclaim their own.
    """
    row = await get_by_owner(owner_id)
    if not row or not row.get("slug"):
        return None
    slug = row["slug"]
    await run_in_thread(_get_table().delete_item, Key={"slug": normalize(slug)})
    return slug


async def scan_all() -> list[dict]:
    """Every row in the table, paginated.

    Only the orphan sweep uses this — a full scan is the ONLY way to find rows
    whose owner no longer exists, because the owner id is gone from everywhere
    that would let you query for it. Not for request-path use.
    """
    table = _get_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
