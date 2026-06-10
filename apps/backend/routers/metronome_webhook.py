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
unknown customer) are fixable by redelivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, Request, Response

from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import webhook_dedup
from core.services.bedrock_enforcement import enforce_on_low_balance
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)

router = APIRouter()

_LOW_BALANCE_EVENT = "alerts.low_remaining_credit_balance_reached"
_MICROCENTS_PER_DOLLAR = 1_000_000


def _verify_signature(*, secret: str, date_header: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    msg = date_header.encode("utf-8") + b"\n" + body
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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

    remaining_dollars = props.get("remaining_balance")
    if remaining_dollars is None:
        logger.warning("Metronome low-balance webhook with no remaining_balance: %s", event_id)
        return Response(status_code=200)

    remaining_microcents = round(float(remaining_dollars) * _MICROCENTS_PER_DOLLAR)
    await MetronomeFastGate().set_balance(owner_id, remaining_microcents=remaining_microcents)
    put_metric(
        "metronome.webhook",
        dimensions={"result": "fast_gate_set", "blocked": str(remaining_microcents <= 0)},
    )
    # Flag first, kill second: even if the kill path errors out (it swallows
    # internally, but belt-and-braces), the gate already blocks new sends.
    await enforce_on_low_balance(owner_id, remaining_microcents)
    return Response(status_code=200)
