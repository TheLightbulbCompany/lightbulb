"""Idempotency helper for inbound webhooks (Stripe primarily, Clerk also).

Reuses the existing `lightbulb-{env}-webhook-event-dedup` DynamoDB table provisioned
by the database stack. Items have a 30-day TTL so the table never grows
unboundedly.

Pattern (per spec §8.4):
    result = await record_event_or_skip(event.id, source="stripe")
    if result is WebhookDedupResult.ALREADY_SEEN:
        return Response(status_code=200)  # silently ack the replay
    # ... process the event ...

Why a separate module: the dedup primitive is dead-simple (one conditional
PutItem) and used by >=2 callers. Keeping it out of the routers means the
idempotency contract is testable in isolation.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)


_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _emit_dedup_metric(source: str, outcome: str) -> None:
    """Best-effort dedup-decision metric. Both dims are bounded (``source``
    is the small provider set stripe/clerk/etc.; ``outcome`` is one of
    recorded/already_seen), so neither explodes the dimension space. Never
    raises — a metrics outage must not break webhook ingestion.
    """
    try:
        from core.observability.metrics import put_metric

        put_metric("webhook.dedup", dimensions={"source": source, "outcome": outcome})
    except Exception:  # noqa: BLE001 — telemetry must never break ingestion
        logger.debug("webhook.dedup metric emission failed", exc_info=True)


class WebhookDedupResult(str, enum.Enum):
    RECORDED = "recorded"
    ALREADY_SEEN = "already_seen"


class WebhookProcessingInProgress(RuntimeError):
    """Another live worker owns this event's retryable processing lease."""


class MembershipLifecycleInProgress(RuntimeError):
    """An older membership event still owns this member's lifecycle lease."""


class MembershipLifecycleSuperseded(RuntimeError):
    """This worker no longer owns the member lifecycle it was processing."""


def membership_lifecycle_key(owner_id: str, alias: str) -> str:
    """Stable central key for one org/member lifecycle, independent of containers."""
    if not isinstance(owner_id, str) or not owner_id or not isinstance(alias, str) or not alias:
        raise ValueError("membership lifecycle identity is required")
    digest = hashlib.sha256(f"{owner_id}\0{alias}".encode()).hexdigest()
    return f"human-membership:{digest}"


def _table():
    """Returns the boto3 Table resource. Created lazily so tests can monkeypatch
    the env var before the first call.

    Fails fast if WEBHOOK_DEDUP_TABLE is empty -- better a clear startup error
    than a confusing boto3 ValidationException on first webhook.

    Reads from os.environ rather than settings so tests can monkeypatch the
    env var after the settings singleton is constructed at import time.
    """
    table_name = os.environ.get("WEBHOOK_DEDUP_TABLE") or settings.WEBHOOK_DEDUP_TABLE
    if not table_name:
        raise RuntimeError(
            "WEBHOOK_DEDUP_TABLE is empty -- backend is misconfigured. "
            "Set the env var via service-stack.ts (already wired)."
        )
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(table_name)


async def delete_event(event_id: str) -> None:
    """Drop the dedup row for `event_id`. Used by callers that wrap
    `record_event_or_skip` in a try/except: if the business-logic handler
    raises after we've recorded the event, the rollback lets the provider's
    retry re-attempt instead of silently skipping. No-op if the row is
    already absent.
    """
    _table().delete_item(Key={"event_id": event_id})


async def has_event(event_id: str) -> bool:
    """Whether `event_id` is currently recorded, WITHOUT claiming it.

    `record_event_or_skip` answers the same question but takes the key as a side
    effect, which is wrong for callers that only want to read — a marker written
    by one flow and consulted by another (see apple_iap's revocation tombstone).
    Strongly consistent, because the caller is deciding entitlement and an
    eventually-consistent miss would grant it.
    """
    response = await run_in_thread(_table().get_item, Key={"event_id": event_id}, ConsistentRead=True)
    return response.get("Item") is not None


async def record_permanent_marker(event_id: str, *, source: str) -> None:
    """Create a no-TTL terminal marker, idempotently.

    This is for deny-state whose safety lifetime is not a provider retry window
    (for example a deleted Clerk subject). The conditional put prevents an
    accidental replay from rewriting marker metadata.
    """
    try:
        await run_in_thread(
            _table().put_item,
            Item={
                "event_id": event_id,
                "source": source,
                "state": "completed",
                "recorded_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def _read_event(event_id: str) -> dict:
    return (
        await run_in_thread(_table().get_item, Key={"event_id": event_id}, ConsistentRead=True)
    ).get("Item", {})


async def begin_event_processing(
    event_id: str,
    *,
    source: str,
    lease_seconds: int = 300,
) -> str | None:
    """Claim a retryable event, returning ``None`` only when it is complete.

    Unlike ``record_event_or_skip``, an in-flight claim is never acknowledged
    as a duplicate. The caller returns a retryable 5xx, then may take over after
    the bounded lease. This removes the unsafe record-then-delete rollback gap.
    """
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    now = int(time.time())
    claim_id = str(uuid.uuid4())
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": event_id},
            UpdateExpression=(
                "SET #state = :processing, claim_id = :claim, lease_until = :lease, "
                "#source = :source, recorded_at = if_not_exists(recorded_at, :now), #ttl = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(event_id) OR "
                "(#state = :processing AND lease_until < :now)"
            ),
            ExpressionAttributeNames={"#state": "state", "#source": "source", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":claim": claim_id,
                ":lease": now + lease_seconds,
                ":source": source,
                ":now": now,
                ":ttl": now + _TTL_SECONDS,
            },
        )
        return claim_id
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    item = await _read_event(event_id)
    # Pre-state-machine rows were written only after the old caller decided to
    # suppress duplicates, so preserve them as completed during rollout.
    if item and item.get("state") in {None, "completed"}:
        return None
    raise WebhookProcessingInProgress(f"webhook event {event_id} is already processing")


async def complete_event_processing(event_id: str, claim_id: str) -> None:
    """Publish completion only while this worker still owns the lease."""
    now = int(time.time())
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": event_id},
            UpdateExpression="SET #state = :completed, completed_at = :now REMOVE claim_id, lease_until",
            ConditionExpression="#state = :processing AND claim_id = :claim",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":completed": "completed",
                ":processing": "processing",
                ":claim": claim_id,
                ":now": now,
            },
        )
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    item = await _read_event(event_id)
    if item.get("state") == "completed":
        return
    raise RuntimeError(f"webhook event {event_id} processing lease was lost")


async def begin_membership_lifecycle(
    owner_id: str,
    alias: str,
    *,
    event_id: str,
    sequence: str,
    access_state: str,
    lease_seconds: int = 300,
) -> str | None:
    """Serialize one member's signed create/delete events in provider order.

    The completed row intentionally has no TTL: it is the membership authority
    even before an owner container exists. A newer event waits while an older
    event owns the lease, then advances the durable epoch after that event
    completes (or after its bounded lease expires following a worker crash).
    """
    if not isinstance(event_id, str) or not event_id or not isinstance(sequence, str) or not sequence:
        raise ValueError("complete membership lifecycle epoch is required")
    if access_state not in {"allowed", "revoked"}:
        raise ValueError("invalid membership lifecycle state")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")

    key = membership_lifecycle_key(owner_id, alias)
    now = int(time.time())
    claim_id = str(uuid.uuid4())
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": key},
            UpdateExpression=(
                "SET #kind = :kind, lifecycle_event_id = :event, #sequence = :sequence, "
                "access_state = :access, lifecycle_status = :processing, claim_id = :claim, "
                "lease_until = :lease, recorded_at = if_not_exists(recorded_at, :now), updated_at = :now "
                "REMOVE completed_at, #ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(event_id) OR "
                "(lifecycle_status = :processing AND lifecycle_event_id = :event "
                "AND #sequence = :sequence AND lease_until < :lease_now) OR "
                "((attribute_not_exists(claim_id) OR lease_until < :lease_now) AND #sequence < :sequence)"
            ),
            ExpressionAttributeNames={"#kind": "kind", "#sequence": "sequence", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":kind": "human-membership",
                ":event": event_id,
                ":sequence": sequence,
                ":access": access_state,
                ":processing": "processing",
                ":claim": claim_id,
                ":lease": now + lease_seconds,
                ":lease_now": now + 1,
                ":now": now,
            },
        )
        return claim_id
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    item = await _read_event(key)
    stored_sequence = item.get("sequence")
    if isinstance(stored_sequence, str) and stored_sequence > sequence:
        return None
    if stored_sequence == sequence and item.get("lifecycle_event_id") == event_id:
        if item.get("lifecycle_status") == "completed":
            return None
        raise MembershipLifecycleInProgress(f"membership event {event_id} is already processing")
    if isinstance(stored_sequence, str) and stored_sequence == sequence:
        return None
    raise MembershipLifecycleInProgress(f"an older membership event for {owner_id} is still processing")


async def renew_membership_lifecycle(
    owner_id: str,
    alias: str,
    claim_id: str,
    *,
    lease_seconds: int = 300,
) -> None:
    """Refresh the lifecycle lease, failing closed after takeover/supersession."""
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    now = int(time.time())
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": membership_lifecycle_key(owner_id, alias)},
            UpdateExpression="SET lease_until = :lease, updated_at = :now",
            ConditionExpression="lifecycle_status = :processing AND claim_id = :claim",
            ExpressionAttributeValues={
                ":processing": "processing",
                ":claim": claim_id,
                ":lease": now + lease_seconds,
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise MembershipLifecycleSuperseded("membership lifecycle lease was lost") from exc


async def complete_membership_lifecycle(owner_id: str, alias: str, claim_id: str) -> None:
    """Publish the permanent access epoch only while this worker owns it."""
    now = int(time.time())
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": membership_lifecycle_key(owner_id, alias)},
            UpdateExpression=(
                "SET lifecycle_status = :completed, completed_at = :now, updated_at = :now "
                "REMOVE claim_id, lease_until, #ttl"
            ),
            ConditionExpression="lifecycle_status = :processing AND claim_id = :claim",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":completed": "completed",
                ":processing": "processing",
                ":claim": claim_id,
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise MembershipLifecycleSuperseded("membership lifecycle lease was lost") from exc


async def get_membership_lifecycle(owner_id: str, alias: str) -> dict | None:
    """Read the central membership authority with strong consistency."""
    item = await _read_event(membership_lifecycle_key(owner_id, alias))
    if not item:
        return None
    marker = {
        "event_id": item.get("lifecycle_event_id"),
        "sequence": item.get("sequence"),
        "state": item.get("access_state"),
    }
    if not (
        item.get("kind") == "human-membership"
        and isinstance(marker["event_id"], str)
        and marker["event_id"]
        and isinstance(marker["sequence"], str)
        and marker["sequence"]
        and marker["state"] in {"allowed", "revoked"}
    ):
        raise RuntimeError("membership lifecycle state unavailable")
    return marker


async def record_event_or_skip(event_id: str, *, source: str, ttl_seconds: int | None = None) -> WebhookDedupResult:
    """Conditionally record a webhook event_id.

    Returns RECORDED on the first call for a given event_id. Returns
    ALREADY_SEEN on every subsequent call. Backed by DynamoDB conditional
    PutItem (`attribute_not_exists(event_id)`).

    Args:
        event_id: provider-issued event id (Stripe `evt_*`, Clerk uuid).
        source: free-form tag stored alongside the row for debugging.
            Does NOT affect dedup keying -- event_id is the sole key.
        ttl_seconds: override the default 30-day retention. Dedup rows only need
            to outlive a provider's retry window, but this table is also used
            for markers whose correctness depends on retention (apple_iap's
            revocation tombstone must outlive the transaction's paid-through
            date, which can exceed 30 days).
    """
    now = int(time.time())
    try:
        _table().put_item(
            Item={
                "event_id": event_id,
                "source": source,
                "recorded_at": now,
                "ttl": now + (ttl_seconds if ttl_seconds is not None else _TTL_SECONDS),
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
        _emit_dedup_metric(source, "recorded")
        return WebhookDedupResult.RECORDED
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            _emit_dedup_metric(source, "already_seen")
            return WebhookDedupResult.ALREADY_SEEN
        logger.warning(
            "webhook dedup PutItem failed (non-conditional) event_id=%s source=%s: %s",
            event_id,
            source,
            e,
        )
        raise
