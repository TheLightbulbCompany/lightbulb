"""Stripe webhook: Spark subscription events (core.services.os_billing)."""

import logging

import stripe
from fastapi import APIRouter, HTTPException, Request, Response

from core.config import settings
from core.observability.metrics import put_metric
from core.services import os_billing

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/webhooks/stripe",
    summary="Handle Stripe webhooks",
    description="Processes Stripe webhook events for Spark subscriptions.",
    operation_id="handle_stripe_webhook",
    include_in_schema=False,
)
async def handle_stripe_webhook(
    request: Request,
):
    """Handle Stripe webhook events. No Clerk auth — uses Stripe signature."""
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(body, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        put_metric("stripe.webhook.sig_fail")
        logger.error("Stripe webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Dedup check — Stripe replays webhooks on any non-2xx and on its own
    # at-least-once delivery insurance. The local import keeps the helper's
    # boto3 client out of cold-start until first use.
    from core.services.webhook_dedup import (
        WebhookDedupResult,
        record_event_or_skip,
    )

    dedup = await record_event_or_skip(event["id"], source="stripe")
    if dedup is WebhookDedupResult.ALREADY_SEEN:
        put_metric(
            "stripe.webhook.dedup_skipped",
            dimensions={"event_type": event["type"]},
        )
        return Response(status_code=200)

    event_type = event["type"]
    put_metric("stripe.webhook.received", dimensions={"event_type": event_type})

    handler = os_billing.stripe_handler(event)
    if handler is None:
        return {"status": "ok"}

    try:
        early_response = await handler(event)
    except Exception:
        # The dedup row was recorded BEFORE dispatch, so a handler failure would otherwise eat the event:
        # the 500 makes Stripe redeliver, and rolling the row back lets the redelivery re-attempt.
        from core.services.webhook_dedup import delete_event

        await delete_event(event["id"])
        raise
    if early_response is not None:
        return early_response
    return {"status": "ok"}
