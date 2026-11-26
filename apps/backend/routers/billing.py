"""Stripe webhook: Spark subscription events (core.services.os_billing)."""

import logging

import stripe
from fastapi import APIRouter, HTTPException, Request

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

    event_type = event["type"]
    put_metric("stripe.webhook.received", dimensions={"event_type": event_type})

    handler = os_billing.stripe_handler(event)
    if handler is None:
        return {"status": "ok"}

    early_response = await handler(event)  # idempotent: re-reads Stripe, writes conditionally
    if early_response is not None:
        return early_response
    return {"status": "ok"}
