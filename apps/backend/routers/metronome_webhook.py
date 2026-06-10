"""POST /webhooks/metronome — the authoritative feed for the local fast-gate flag.

Metronome's balance is async (no low-latency SLA), so the synchronous chat
gate reads the local fast-gate flag instead. This webhook sets that flag on
alerts.low_remaining_credit_balance_reached and — when enforcement is armed
(settings.BEDROCK_ENFORCEMENT_LIVE) — fires the Bedrock kill-switch via
enforce_on_low_balance. There is no backend deplete; between webhook fires
the flag simply holds its last value, with the kill-switch as the backstop.

Signature: Metronome-Webhook-Signature = hex HMAC_SHA256(secret, Date + "\n" +
raw_body). Deduped by event id via the shared webhook_dedup helper.

Every handled-but-unprocessable shape returns 200: a non-2xx makes Metronome
redeliver the same event forever, and none of these gaps (missing fields,
unknown customer) are fixable by redelivery. A processing EXCEPTION is the
opposite case — transient and fixable — so it rolls the dedup row back and
re-raises, letting redelivery re-attempt instead of dropping the signal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, Response

from core.billing.metronome_fast_gate import MetronomeFastGate
from core.billing.metronome_units import cents_to_microcents
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import webhook_dedup
from core.services.bedrock_enforcement import enforce_on_low_balance
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)

router = APIRouter()

_LOW_BALANCE_EVENT = "alerts.low_remaining_credit_balance_reached"


def _verify_signature(*, secret: str, date_header: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    msg = date_header.encode("utf-8") + b"\n" + body
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_timestamp(value: object) -> datetime | None:
    """Best-effort ISO-8601 parse for the staleness comparison. None on any
    unparseable shape — the caller then processes the event normally (a
    garbled timestamp must not drop the one low-balance signal)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.post("/webhooks/metronome")
async def handle_metronome_webhook(
    request: Request,
    metronome_signature: str = Header("", alias="Metronome-Webhook-Signature"),
    date_header: str = Header("", alias="Date"),
) -> Response:
    body = await request.body()
    if not _verify_signature(
        secret=settings.METRONOME_WEBHOOK_SECRET,
        date_header=date_header,
        body=body,
        signature=metronome_signature,
    ):
        put_metric("metronome.webhook", dimensions={"result": "bad_signature"})
        return Response(status_code=401)

    event = json.loads(body)
    event_id = event.get("id")
    if not event_id:
        return Response(status_code=200)

    dedup = await webhook_dedup.record_event_or_skip(event_id=event_id, source="metronome")
    if dedup == WebhookDedupResult.ALREADY_SEEN:
        return Response(status_code=200)

    try:
        return await _process_event(event, event_id)
    except Exception:
        # Alerts fire ONCE per threshold crossing — a processing failure
        # (transient DDB/billing-repo blip) would otherwise permanently drop
        # the one low-balance signal, because the dedup row already marks the
        # event as seen. Roll the dedup row back so Metronome's redelivery
        # gets to re-attempt.
        await webhook_dedup.delete_event(event_id)
        raise


async def _process_event(event: dict, event_id: str) -> Response:
    if event.get("type") != _LOW_BALANCE_EVENT:
        return Response(status_code=200)

    props = event.get("properties") or {}
    # The alert payload identifies the customer by the METRONOME UUID — the
    # ingest-alias trick (customer_id == owner_id) applies to /ingest events
    # only, never to alert webhooks — so the owner is resolved through the
    # billing row where ensure_metronome_customer persisted the UUID.
    customer_id = props.get("customer_id")
    if not customer_id:
        logger.warning("Metronome low-balance webhook with no customer_id: %s", event_id)
        return Response(status_code=200)

    account = await billing_repo.get_by_metronome_customer_id(customer_id)
    if account is None:
        put_metric("metronome.webhook", dimensions={"result": "unknown_customer"})
        logger.warning(
            "Metronome low-balance webhook for unknown customer %s (event %s) — no billing row carries this UUID",
            customer_id,
            event_id,
        )
        return Response(status_code=200)
    owner_id = account["owner_id"]

    # remaining_balance is Metronome's USD credit type, denominated in CENTS
    # (see core/billing/metronome_units.py).
    remaining_cents = props.get("remaining_balance")
    if remaining_cents is None:
        logger.warning("Metronome low-balance webhook with no remaining_balance: %s", event_id)
        return Response(status_code=200)

    remaining_microcents = cents_to_microcents(remaining_cents)
    gate = MetronomeFastGate()

    # Staleness guard: a low-balance webhook delivered LATE (Metronome
    # redelivery after our earlier non-2xx, or plain network delay) can
    # arrive after a confirmed top-up already re-armed the flag. Applying it
    # would stomp the re-arm and re-kill a paid-up owner — and a blocked
    # owner emits no usage, so no corrective webhook would ever come. The
    # fast-gate row's updated_at moves on every set_balance/add_credit, so a
    # row newer than the event's own timestamp means the event is old news.
    event_ts = _parse_timestamp(props.get("timestamp"))
    if event_ts is not None:
        row = await gate.get_row(owner_id)
        row_ts = _parse_timestamp((row or {}).get("updated_at"))
        if row_ts is not None and row_ts > event_ts:
            put_metric("metronome.webhook.stale_skipped")
            logger.info(
                "Skipping stale Metronome low-balance event %s for owner %s (event ts %s predates fast-gate row ts %s)",
                event_id,
                owner_id,
                event_ts.isoformat(),
                row_ts.isoformat(),
            )
            return Response(status_code=200)

    await gate.set_balance(owner_id, remaining_microcents=remaining_microcents)
    put_metric(
        "metronome.webhook",
        dimensions={"result": "fast_gate_set", "blocked": str(remaining_microcents <= 0)},
    )
    # Flag first, kill second: even if the kill path errors out (it swallows
    # internally, but belt-and-braces), the gate already blocks new sends.
    await enforce_on_low_balance(owner_id, remaining_microcents)
    return Response(status_code=200)
