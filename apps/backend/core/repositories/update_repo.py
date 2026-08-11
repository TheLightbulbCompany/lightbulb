"""Pending-updates repository -- DynamoDB CRUD for the pending-updates table."""

import time
import uuid
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Attr, Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

# An 'applying' row older than this is a crashed/replaced applier, not a live
# roll: apply_update writes its config patch + issues the ECS re-register and
# returns WITHOUT waiting for the new task to go healthy, so a real apply
# finishes in seconds. 30 min is deliberately far past that — a row applying
# this long is genuinely dead. Used ONLY by the admin force-apply path (see
# is_stale_applying); there is intentionally NO automatic concurrent takeover.
STALE_APPLYING_SECONDS = 30 * 60


def is_stale_applying(row: dict) -> bool:
    """True if ``row`` is wedged in 'applying' past ``STALE_APPLYING_SECONDS``.

    Staleness is keyed on ``updated_at`` — set on the pending->applying
    transition and NOT rewritten until the terminal mark_applied/mark_failed —
    so its age is exactly how long the row has sat in 'applying'.

    This exists for ONE caller: the admin force-apply guard, a human-triggered
    action. A wedged 'applying' row otherwise 409s every future roll for the
    owner forever (CC-3). We do NOT auto-take-over such a row anywhere on the
    automatic (scheduled / owner-banner) paths: a paused-then-resumed applier
    would still run its ECS side effects, and no DB fence can undo a side effect
    that already fired. Clearing is gated behind an explicit operator force-apply
    where a human owns the "the old applier is dead" judgement.

    Conservative on a missing ``updated_at``: returns False (treat as live,
    never stomp) — a genuinely-live legacy 'applying' row always carries a
    recent ``updated_at`` from its own transition, so this only ever guards the
    (unexpected) no-timestamp case.
    """
    if row.get("status") != "applying":
        return False
    updated_at = row.get("updated_at")
    if not updated_at:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_APPLYING_SECONDS)).isoformat()
    return updated_at < cutoff


def _get_table():
    return get_table("pending-updates")


def _generate_ulid_like() -> str:
    """Generate a ULID-like ID: timestamp prefix + random suffix for sortability.

    Uses millisecond timestamp (base32-ish hex) + uuid4 hex to approximate
    ULID ordering without requiring the python-ulid dependency.
    """
    ts_ms = int(time.time() * 1000)
    ts_hex = f"{ts_ms:012x}"
    rand_hex = uuid.uuid4().hex[:20]
    return f"{ts_hex}{rand_hex}"


async def create(
    owner_id: str,
    update_type: str,
    description: str,
    changes: dict,
    force_by: str | None = None,
) -> dict:
    """Create a new pending update with a ULID-like ID."""
    table = _get_table()
    now = utc_now_iso()
    update_id = _generate_ulid_like()
    item: dict = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": update_type,
        "description": description,
        "changes": changes,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    if force_by is not None:
        item["force_by"] = force_by
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_pending(owner_id: str) -> list[dict]:
    """Get all pending or scheduled updates for an owner."""
    table = _get_table()
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        FilterExpression=Attr("status").is_in(["pending", "scheduled"]),
    )
    return response.get("Items", [])


async def get_in_flight(owner_id: str) -> list[dict]:
    """Return an owner's non-terminal updates: pending, scheduled, or the transient
    'applying' state.

    Superset of get_pending() (which omits 'applying'). Callers use it to enforce
    per-owner mutual exclusion on container rolls — apply_update's acquire is keyed
    per-update_id, so this is the only way to know an owner already has an update
    queued or mid-apply and must not have a second roll started concurrently.
    Callers decide which rows actually conflict (e.g. those whose ``changes`` rewrite
    the task-def) rather than trusting the update_type label.

    Reads are strongly consistent: the guard must see a row written moments earlier
    (a just-queued update, or a concurrent apply that already flipped to
    'applying') rather than risk an eventually-consistent miss.

    Paginated: the status FilterExpression is applied AFTER DynamoDB's 1 MB page
    read, so an owner with enough historical terminal rows could otherwise push a
    newer in-flight row onto a later page and hide it from the guard.
    """
    table = _get_table()
    items: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "FilterExpression": Attr("status").is_in(["pending", "scheduled", "applying"]),
            "ConsistentRead": True,
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items


async def set_status_conditional(
    owner_id: str,
    update_id: str,
    new_status: str,
    expected_statuses: list[str],
) -> bool:
    """Conditionally update status. Returns True on success, False if condition fails."""
    table = _get_table()
    now = utc_now_iso()

    # Build condition: status must be one of expected_statuses
    # DynamoDB doesn't support IN in ConditionExpression directly with update_item,
    # so we build an OR chain.
    condition = Attr("status").eq(expected_statuses[0])
    for s in expected_statuses[1:]:
        condition = condition | Attr("status").eq(s)

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :new_status, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":new_status": new_status, ":now": now},
            ConditionExpression=condition,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def set_scheduled(
    owner_id: str,
    update_id: str,
    scheduled_at: str,
) -> bool:
    """Set an update to scheduled status with a scheduled_at timestamp."""
    table = _get_table()
    now = utc_now_iso()
    condition = Attr("status").eq("pending")

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :status, scheduled_at = :sat, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "scheduled",
                ":sat": scheduled_at,
                ":now": now,
            },
            ConditionExpression=condition,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def set_snoozed(owner_id: str, update_id: str) -> bool:
    """Record that the update was snoozed (sets last_snoozed_at)."""
    table = _get_table()
    now = utc_now_iso()

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET last_snoozed_at = :now, updated_at = :now2",
            ExpressionAttributeValues={":now": now, ":now2": now},
            ConditionExpression=Attr("owner_id").exists(),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def get_due_scheduled() -> list[dict]:
    """Query GSI for scheduled updates that are due (scheduled_at <= now)."""
    table = _get_table()
    now = utc_now_iso()
    response = await run_in_thread(
        table.query,
        IndexName="status-index",
        KeyConditionExpression=(Key("status").eq("scheduled") & Key("scheduled_at").lte(now)),
    )
    return response.get("Items", [])


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all pending-update rows for an owner. Returns count deleted.

    Used by the e2e teardown endpoint. PK=owner_id, SK=update_id.
    Paginates the DDB query so multi-page results don't leak.
    """
    table = _get_table()
    deleted = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "ProjectionExpression": "owner_id, update_id",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        for item in response.get("Items", []):
            await run_in_thread(
                table.delete_item,
                Key={"owner_id": item["owner_id"], "update_id": item["update_id"]},
            )
            deleted += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted


async def count_for_owner(owner_id: str) -> int:
    """Count pending-update rows for an owner. Used by /debug/ddb-rows.

    Paginates so the count matches reality past the 1MB query boundary.
    """
    table = _get_table()
    total = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "Select": "COUNT",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        total += int(response.get("Count", 0))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return total


async def mark_applied(owner_id: str, update_id: str) -> bool:
    """Mark an update as applied and set TTL for 30-day expiry."""
    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)  # 30 days

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :status, applied_at = :now, updated_at = :now2, #t = :ttl",
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={
                ":status": "applied",
                ":now": now,
                ":now2": now,
                ":ttl": ttl_epoch,
            },
            # "applying" MUST be accepted: apply_update transitions the row
            # pending/scheduled -> applying BEFORE doing the work and calling
            # this, so at this call site the row is "applying". Omitting it left
            # the condition unsatisfiable there, so the row was never flipped to
            # "applied" and stayed stuck in "applying" forever (the ECS change
            # had already taken effect). "pending"/"scheduled" stay for callers
            # that mark a row applied without an intervening "applying" hop.
            ConditionExpression=Attr("status").is_in(["applying", "pending", "scheduled"]),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def list_pending_by_type(update_type: str) -> list[dict]:
    """Scan the table for pending rows of a given ``update_type``.

    Used by the T13 cleanup cron's provisioning-retry pass — rows
    enqueued by Clerk webhooks (T12) carry ``update_type="paperclip_provision"``
    and need to be picked up regardless of which ``owner_id`` partition
    they live in.

    Implementation: a paginated scan with FilterExpression. The
    ``status-index`` GSI is keyed on ``status`` HASH + ``scheduled_at``
    RANGE; rows without ``scheduled_at`` (which is the case for
    webhook-enqueued retries) are excluded from that index, so we
    cannot use it here. A scan is acceptable because the cron runs
    infrequently and the pending-updates table stays small (TTL purges
    applied rows after 30 days).
    """
    table = _get_table()
    out: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "FilterExpression": Attr("status").eq("pending") & Attr("update_type").eq(update_type),
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.scan, **kwargs)
        out.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return out


async def mark_failed(owner_id: str, update_id: str, reason: str) -> bool:
    """Mark a pending row as failed with a truncated reason string.

    Used by the T13 retry pass when a Paperclip provisioning op
    raises a non-retryable exception (or hits an unknown ``op``).
    Sets a 30-day TTL like ``mark_applied`` so failed rows don't
    accumulate indefinitely.
    """
    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    truncated = (reason or "")[:1000]

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=("SET #s = :status, failure_reason = :reason, updated_at = :now, #t = :ttl"),
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={
                ":status": "failed",
                ":reason": truncated,
                ":now": now,
                ":ttl": ttl_epoch,
            },
            ConditionExpression=Attr("status").is_in(["pending", "scheduled", "applying"]),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
