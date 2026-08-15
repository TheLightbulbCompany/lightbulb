"""Idempotency helper for inbound webhooks (Stripe primarily, Clerk also).

Reuses the existing `isol8-{env}-webhook-event-dedup` DynamoDB table provisioned
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
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from core.config import settings

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
    response = _table().get_item(Key={"event_id": event_id}, ConsistentRead=True)
    return response.get("Item") is not None


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
