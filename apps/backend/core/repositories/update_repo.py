"""Pending-updates repository -- DynamoDB CRUD for the pending-updates table."""

import time
import uuid

from boto3.dynamodb.conditions import Attr, Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("pending-updates")


_UNRESERVED_UPDATE = Attr("release_revision").not_exists() & (
    Attr("update_type").not_exists() | Attr("update_type").ne("release")
)

_OWNER_APPLY_CONTAINER_PINS = (
    "id",
    "status",
    "desired_release_id",
    "active_release_id",
    "release_revision",
)
_OWNER_APPLY_CONTAINER_EXCLUSIONS = (
    "release_update_id",
    "provision_claim_id",
    "provision_attempt_token",
    "provision_execution_name",
)
_OWNER_APPLY_UPDATE_PINS = (
    "status",
    "update_type",
    "changes",
    "scheduled_at",
    "last_snoozed_at",
    "force_by",
)
_OWNER_APPLY_UPDATE_EXCLUSIONS = (
    "release_revision",
    "container_id",
    "apply_claim_id",
    "apply_claimed_at",
    "phase",
    "target_release_id",
    "prior_release_id",
    "prior_desired_release_id",
    "prior_desired_count",
    "target_desired_count",
    "staged_config_sha256",
    "prior_config_sha256",
    "effect_kind",
    "effect_claim_id",
    "effect_executor_id",
    "applied_at",
    "failure_reason",
)


def _require_nonempty_string(field: str, value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} is invalid")
    return value


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
    scheduled_at: str | None = None,
) -> dict:
    """Create a pending update, or an atomically scheduled update when set."""
    table = _get_table()
    now = utc_now_iso()
    update_id = _generate_ulid_like()
    item: dict = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": update_type,
        "description": description,
        "changes": changes,
        "status": "scheduled" if scheduled_at is not None else "pending",
        "created_at": now,
        "updated_at": now,
    }
    if force_by is not None:
        item["force_by"] = force_by
    if scheduled_at is not None:
        item["scheduled_at"] = scheduled_at
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_by_id(owner_id: str, update_id: str, *, consistent: bool = False) -> dict | None:
    """Read one update, optionally strongly consistently."""

    _require_nonempty_string("owner_id", owner_id)
    _require_nonempty_string("update_id", update_id)
    table = _get_table()
    response = await run_in_thread(
        table.get_item,
        Key={"owner_id": owner_id, "update_id": update_id},
        ConsistentRead=consistent,
    )
    return response.get("Item")


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


async def claim_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
) -> dict | None:
    """Atomically claim a pending/scheduled update and return its new row.

    ``apply_claim_id`` is a fencing token. Long-running callers must use the
    claim-aware target and terminal helpers below so a worker superseded after
    backend turnover cannot mutate the successor's row.
    """

    table = _get_table()
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    try:
        response = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=(
                "SET #s = :applying, apply_claim_id = :claim, apply_claimed_at = :claimed, updated_at = :updated"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":applying": "applying",
                ":claim": claim_id,
                ":claimed": now_epoch,
                ":updated": now_iso,
            },
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["pending", "scheduled"]),
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None


async def claim_stale_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
    *,
    stale_before_epoch: int,
) -> dict | None:
    """Take over an abandoned applying row, returning the newly fenced row."""

    table = _get_table()
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    try:
        response = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=("SET apply_claim_id = :claim, apply_claimed_at = :claimed, updated_at = :updated"),
            ExpressionAttributeValues={
                ":claim": claim_id,
                ":claimed": now_epoch,
                ":updated": now_iso,
            },
            ConditionExpression=(
                _UNRESERVED_UPDATE
                & Attr("status").eq("applying")
                & (Attr("apply_claimed_at").not_exists() | Attr("apply_claimed_at").lte(stale_before_epoch))
            ),
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None


async def heartbeat_apply(owner_id: str, update_id: str, claim_id: str) -> bool:
    """Refresh an apply lease iff ``claim_id`` still owns the row."""

    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET apply_claimed_at = :claimed, updated_at = :updated",
            ExpressionAttributeValues={
                ":claimed": int(time.time()),
                ":updated": utc_now_iso(),
            },
            ConditionExpression=(
                _UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id)
            ),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def finish_claimed_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
    *,
    status: str,
    failure_reason: str | None = None,
) -> bool:
    """Write a terminal apply result iff the caller still owns the claim."""

    if status not in {"applied", "failed"}:
        raise ValueError("claimed apply terminal status must be applied or failed")

    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    names = {"#s": "status", "#t": "ttl"}
    values = {
        ":status": status,
        ":now": now,
        ":ttl": ttl_epoch,
    }
    set_parts = ["#s = :status", "updated_at = :now", "#t = :ttl"]
    if status == "applied":
        set_parts.append("applied_at = :now")
    else:
        set_parts.append("failure_reason = :reason")
        values[":reason"] = (failure_reason or "")[:1000]

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=(
                _UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id)
            ),
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
    condition = _UNRESERVED_UPDATE & Attr("status").eq("pending")

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
            ConditionExpression=_UNRESERVED_UPDATE & Attr("owner_id").exists(),
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


async def list_stale_applying(update_type: str, *, stale_before_epoch: int) -> list[dict]:
    """Return every abandoned applying row of ``update_type``.

    This deliberately scans rather than using ``status-index``: older applying
    rows and force-applied rows may have no ``scheduled_at`` and therefore do
    not exist in that GSI. The pending-updates table is TTL-bounded, and this
    recovery pass runs once per worker tick rather than on a request hot path.
    """

    table = _get_table()
    items: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "FilterExpression": (
                Attr("status").eq("applying")
                & Attr("update_type").eq(update_type)
                & (Attr("apply_claimed_at").not_exists() | Attr("apply_claimed_at").lte(stale_before_epoch))
            ),
            "ConsistentRead": True,
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.scan, **kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items


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
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["applying", "pending", "scheduled"]),
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
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["pending", "scheduled", "applying"]),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
