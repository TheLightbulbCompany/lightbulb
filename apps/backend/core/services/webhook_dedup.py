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
import functools
import hashlib
import logging
import os
import time
import uuid
from typing import Callable

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

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


class TopUpIntentConflict(RuntimeError):
    """A caller reused a top-up idempotency token with different money inputs."""


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


async def _read_event(event_id: str) -> dict:
    return (await run_in_thread(_table().get_item, Key={"event_id": event_id}, ConsistentRead=True)).get("Item", {})


def top_up_intent_key(owner_id: str, request_id: str) -> str:
    """Permanent idempotency identity for one workspace credit purchase."""
    if not isinstance(owner_id, str) or not owner_id or not isinstance(request_id, str) or not request_id:
        raise ValueError("workspace and request_id are required")
    return f"metronome-top-up:{owner_id}:{request_id}"


def active_top_up_key(owner_id: str) -> str:
    """Permanent one-per-workspace pointer for a nonterminal top-up."""
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("workspace is required")
    return f"metronome-top-up-active:{owner_id}"


_TOP_UP_TERMINAL_STATUSES = frozenset({"paid", "failed"})


async def get_active_top_up(owner_id: str) -> dict | None:
    """Return the workspace's durable top-up pointer, including terminal state."""
    item = await _read_event(active_top_up_key(owner_id))
    if not item:
        return None
    if item.get("kind") != "metronome-top-up-active" or item.get("owner_id") != owner_id:
        raise TopUpIntentConflict("active top-up identity is unavailable")
    return item


async def get_top_up_intent(owner_id: str, request_id: str) -> dict | None:
    """Return one permanent intent, scoped by its workspace and request UUID."""
    item = await _read_event(top_up_intent_key(owner_id, request_id))
    if not item:
        return None
    if (
        item.get("kind") != "metronome-top-up"
        or item.get("owner_id") != owner_id
        or item.get("request_id") != request_id
        or item.get("state") not in {"pending", "completed", "paid", "failed"}
    ):
        raise TopUpIntentConflict("top-up intent identity is unavailable")
    return item


async def set_active_top_up_status(
    owner_id: str,
    metronome_customer_id: str,
    metronome_contract_id: str,
    status: str,
    *,
    intent_event_id: str | None = None,
) -> bool:
    """Advance a matching active pointer without terminally settling it."""
    if status not in {"requested", "action_required"}:
        raise ValueError("invalid active top-up status")
    active = await get_active_top_up(owner_id)
    if (
        active is None
        or active.get("metronome_customer_id") != metronome_customer_id
        or active.get("metronome_contract_id") != metronome_contract_id
        or (intent_event_id is not None and active.get("intent_event_id") != intent_event_id)
    ):
        return False
    if active.get("status") in _TOP_UP_TERMINAL_STATUSES:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": active_top_up_key(owner_id)},
            UpdateExpression="SET #status = :status, updated_at = :now",
            ConditionExpression="#kind = :kind AND #intent = :intent AND #status = :current",
            ExpressionAttributeNames={"#status": "status", "#kind": "kind", "#intent": "intent_event_id"},
            ExpressionAttributeValues={
                ":status": status,
                ":current": active["status"],
                ":now": int(time.time()),
                ":kind": "metronome-top-up-active",
                ":intent": active["intent_event_id"],
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def settle_active_top_up(
    owner_id: str,
    metronome_customer_id: str,
    metronome_contract_id: str,
    status: str,
    *,
    invoice_id: str,
) -> bool:
    """Bind an invoice permanently before settling its exact purchase intent.

    Every retry resumes this binding, including after the active pointer has
    been replaced. The marker has no TTL: a later webhook for an old invoice
    must never settle a new purchase.
    """
    if status not in _TOP_UP_TERMINAL_STATUSES:
        raise ValueError("invalid terminal top-up status")
    if not isinstance(invoice_id, str) or not invoice_id:
        raise ValueError("invoice_id is required")
    key = f"metronome-top-up-invoice:{invoice_id}"
    marker = await _read_event(key)
    if not marker:
        active = await get_active_top_up(owner_id)
        if (
            active is None
            or active.get("metronome_customer_id") != metronome_customer_id
            or active.get("metronome_contract_id") != metronome_contract_id
            or active.get("status") in _TOP_UP_TERMINAL_STATUSES
        ):
            return False
        marker = {
            "event_id": key,
            "kind": "metronome-top-up-invoice",
            "state": "processing",
            "owner_id": owner_id,
            "invoice_id": invoice_id,
            "intent_event_id": active["intent_event_id"],
            "request_id": active["request_id"],
            "metronome_customer_id": metronome_customer_id,
            "metronome_contract_id": metronome_contract_id,
            "status": status,
            "recorded_at": int(time.time()),
        }
        try:
            await run_in_thread(
                _table().put_item,
                Item=marker,
                ConditionExpression="attribute_not_exists(event_id)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            marker = await _read_event(key)

    if (
        marker.get("kind") != "metronome-top-up-invoice"
        or marker.get("owner_id") != owner_id
        or marker.get("metronome_customer_id") != metronome_customer_id
        or marker.get("metronome_contract_id") != metronome_contract_id
    ):
        raise TopUpIntentConflict("top-up invoice binding changed")
    if marker.get("state") == "completed":
        return False
    # The first terminal event wins, even if a conflicting status arrives
    # while that event is still being processed.
    status = marker["status"]

    await run_in_thread(
        _table().update_item,
        Key={"event_id": marker["intent_event_id"]},
        UpdateExpression="SET #state = :status, settled_at = if_not_exists(settled_at, :now)",
        ConditionExpression=(
            "#kind = :kind AND owner_id = :owner AND request_id = :request "
            "AND metronome_customer_id = :customer AND metronome_contract_id = :contract "
            "AND (#state = :pending OR #state = :completed OR #state = :status)"
        ),
        ExpressionAttributeNames={"#state": "state", "#kind": "kind"},
        ExpressionAttributeValues={
            ":status": status,
            ":now": int(time.time()),
            ":kind": "metronome-top-up",
            ":owner": owner_id,
            ":request": marker["request_id"],
            ":customer": metronome_customer_id,
            ":contract": metronome_contract_id,
            ":pending": "pending",
            ":completed": "completed",
        },
    )
    try:
        await run_in_thread(
            _table().update_item,
            Key={"event_id": active_top_up_key(owner_id)},
            UpdateExpression="SET #status = :status, settled_at = if_not_exists(settled_at, :now)",
            ConditionExpression="#kind = :kind AND intent_event_id = :intent",
            ExpressionAttributeNames={"#status": "status", "#kind": "kind"},
            ExpressionAttributeValues={
                ":status": status,
                ":now": int(time.time()),
                ":kind": "metronome-top-up-active",
                ":intent": marker["intent_event_id"],
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # A newer pointer may already exist; the immutable intent above is
        # still the sole target of this invoice's replay.
    await run_in_thread(
        _table().update_item,
        Key={"event_id": key},
        UpdateExpression="SET #state = :completed",
        ConditionExpression="intent_event_id = :intent",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":completed": "completed", ":intent": marker["intent_event_id"]},
    )
    return True


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
            ConditionExpression=("attribute_not_exists(event_id) OR (#state = :processing AND lease_until < :now)"),
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


async def membership_revocation_current(owner_id: str, alias: str, event_id: str) -> bool:
    """True while this delete event is still the member's latest lifecycle epoch (processing
    or completed). A later create -- a re-add -- replaces the epoch, and this turns False."""
    item = await _read_event(membership_lifecycle_key(owner_id, alias))
    return item.get("lifecycle_event_id") == event_id and item.get("access_state") == "revoked"


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
        await run_in_thread(
            _table().put_item,
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


def idempotency(*, header: str = "Idempotency-Key", ttl_s: int = 60) -> Callable:
    """Refuse a repeated ``Idempotency-Key`` on an admin write handler with 409.

    Replaces the in-memory TTL cache that used to live in its own predecessor
    module: a module-level dict is per-instance, so at desiredCount >= 2 two
    clicks landing on two tasks BOTH executed. The repo's own
    ``container_repo.try_claim_owner_deploy`` docstring made this argument
    long before anyone acted on it.

    **Contract change.** A repeat key used to re-serve the first response; it is
    now refused. Preserving replay means persisting response bodies, and the
    twelve handlers behind this are container / billing / fleet mutations whose
    services are individually claim- or condition-guarded -- the value was always
    "don't run it twice", never "give me the same body twice".

    Fails CLOSED: a dedup-table fault raises and the request fails. An admin
    mutation that cannot be deduped should not run. Requests with no header
    bypass entirely, as before.

    A handler that raises keeps its claim: the record was already written
    before the handler ran, so a same-key retry 409s until that claim's TTL
    expires -- deliberate, since releasing the claim on failure reintroduces
    the double-click race this decorator exists to close. The 409 means "this
    key was already claimed", not "the mutation succeeded".
    """

    def decorator(handler: Callable) -> Callable:
        handler_id = f"{handler.__module__}.{handler.__qualname__}"

        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            key = request.headers.get(header) if request is not None and hasattr(request, "headers") else None
            if not key:
                return await handler(*args, **kwargs)
            seen = await record_event_or_skip(f"admin:{handler_id}:{key}", source="admin", ttl_seconds=ttl_s)
            if seen is WebhookDedupResult.ALREADY_SEEN:
                raise HTTPException(status_code=409, detail="duplicate Idempotency-Key")
            return await handler(*args, **kwargs)

        return wrapped

    return decorator
