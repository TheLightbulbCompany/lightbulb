"""POST /webhooks/metronome — the authoritative feed for the local fast-gate flag.

Metronome's balance is async (no low-latency SLA), so the synchronous chat
gate reads the local fast-gate flag instead. This webhook sets that flag on
the low-balance alert event types. There is no backend deplete; between
webhook fires the flag simply holds its last value.

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
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)

router = APIRouter()

# The alerts we create are commit-balance alerts (sandbox-verified type:
# low_remaining_commit_balance_reached — our balance is prepaid commits on a
# contract). The webhook EVENT-type string is unverifiable until a delivery
# actually fires, and Metronome's alert-create enum vs webhook event-type
# naming is not documented as 1:1 — so match a set of the plausible spellings.
# Set membership is cheap, and dropping a delivery on a wrong single-string
# match would permanently lose the one low-balance signal (alerts fire once
# per threshold crossing).
_LOW_BALANCE_EVENT_TYPES = frozenset(
    {
        "alerts.low_remaining_commit_balance_reached",
        "alerts.low_remaining_credit_balance_reached",
        "alerts.low_remaining_contract_credit_and_commit_balance_reached",
    }
)

# Payment-gated top-up commits charge the saved card; when the charge fails or
# needs SCA/3DS, Metronome emits a payment-gate event and does NOT auto-retry,
# so the credits never release. We can't complete card auth server-side, so the
# minimum bar is OBSERVABILITY — these must not vanish into a silent 200 like
# every other unmodeled type. The exact spelling is unverifiable until a real
# delivery fires (same caveat as the low-balance set), so match plausible
# spellings; the matching customer/contract is then reconciled against the
# permanent active top-up pointer.
_PAYMENT_GATE_EVENT_TYPES = frozenset(
    {
        "payment_gate.payment_status",
        "payment_gate.payment_pending_action_required",
        "alerts.payment_gate.payment_status",
        "alerts.payment_gate.payment_pending_action_required",
    }
)


def _verify_signature(*, secret: str, date_header: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    msg = date_header.encode("utf-8") + b"\n" + body
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    # Compare bytes, not str: compare_digest raises TypeError on non-ASCII str.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


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
    event_type = event.get("type")
    if event_type in _PAYMENT_GATE_EVENT_TYPES:
        # payment_gate.payment_status fires after EVERY attempt carrying
        # payment_status = "paid" | "failed"; pending-action fires for SCA/3DS
        # (Metronome docs: manual-payment-gated-commits#manage-notifications).
        # Only the non-paid outcomes need attention — a "paid" event is a
        # normal successful top-up and must NOT warn/page (it would turn every
        # purchase into operational noise). Either way it's COUNTED (dimensioned
        # by status) so a silent 200 never hides a failure. A missing/unknown
        # status degrades to the attention branch (warn, never silently drop) —
        # the conservative default for a money-path signal.
        props = event.get("properties") or {}
        payment_status = props.get("payment_status")
        put_metric("metronome.webhook.payment_gate", dimensions={"status": payment_status or "action_required"})
        customer_id = props.get("customer_id")
        contract_id = props.get("contract_id")
        if not isinstance(customer_id, str) or not isinstance(contract_id, str):
            logger.warning("Metronome payment-gate event %s has no customer/contract binding", event_id)
            return Response(status_code=200)
        account = await billing_repo.get_by_metronome_customer_id(customer_id)
        if account is None:
            put_metric("metronome.webhook", dimensions={"result": "unknown_customer"})
            logger.warning("Metronome payment-gate event %s has unknown customer %s", event_id, customer_id)
            return Response(status_code=200)
        owner_id = account["owner_id"]
        invoice_id = props.get("invoice_id")
        if payment_status in {"paid", "failed"} and (not isinstance(invoice_id, str) or not invoice_id):
            logger.warning("Metronome payment-gate event %s has no invoice binding", event_id)
            return Response(status_code=200)
        if payment_status == "paid":
            settled = await webhook_dedup.settle_active_top_up(
                owner_id, customer_id, contract_id, "paid", invoice_id=invoice_id
            )
            logger.info(
                "Metronome payment-gate paid (id=%s, customer=%s, matched=%s)",
                event_id,
                customer_id,
                settled,
            )
        elif payment_status == "failed":
            settled = await webhook_dedup.settle_active_top_up(
                owner_id, customer_id, contract_id, "failed", invoice_id=invoice_id
            )
            logger.warning(
                "Metronome payment-gate failed (id=%s, customer=%s, matched=%s)",
                event_id,
                customer_id,
                settled,
            )
        else:
            matched = await webhook_dedup.set_active_top_up_status(
                owner_id,
                customer_id,
                contract_id,
                "action_required",
            )
            logger.warning(
                "Metronome payment-gate %s (id=%s, customer=%s, status=%s, matched=%s) — gated top-up payment needs attention",
                event_type,
                event_id,
                customer_id,
                payment_status,
                matched,
            )
        return Response(status_code=200)

    if event_type not in _LOW_BALANCE_EVENT_TYPES:
        # Every other delivery (invoice.*, and any future type) is acked, but
        # COUNTED — a silent 200 made payment-gate failures and config drift
        # invisible. The metric is the breadcrumb that tells us a type we don't
        # model yet is actually arriving.
        put_metric("metronome.webhook.unhandled", dimensions={"type": event_type or "none"})
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

    try:
        remaining_microcents = cents_to_microcents(remaining_cents)
    except (TypeError, ValueError):
        # A non-numeric remaining_balance is permanently garbled — letting it
        # raise into the catch-all would roll the dedup row back and put the
        # same unprocessable event on infinite redelivery. Keep the dedup row
        # and ack: redelivery cannot fix the payload.
        put_metric("metronome.webhook.malformed")
        logger.warning(
            "Metronome low-balance webhook %s has non-numeric remaining_balance %r — acking without processing",
            event_id,
            remaining_cents,
        )
        return Response(status_code=200)

    gate = MetronomeFastGate()

    # Staleness guard: a low-balance webhook delivered LATE (Metronome
    # redelivery after our earlier non-2xx, or plain network delay) can
    # arrive after a gate-refresh already re-armed the flag. Applying it
    # would stomp the re-arm and re-kill a paid-up owner — and a blocked
    # owner emits no usage, so no corrective webhook would ever come. The
    # fast-gate row's updated_at moves on every set_balance (webhook or lazy
    # gate refresh), so a
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
    return Response(status_code=200)
