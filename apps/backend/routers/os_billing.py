"""Spark's two billing links. They take no Clerk session: they are opened in the person's browser and carry
their own short-lived token (core.services.os_billing), and answer 303 to Stripe or to the billing page."""

import logging

import stripe
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from core.services import os_billing

logger = logging.getLogger(__name__)
router = APIRouter()


async def _open(kind: str, t: str, make) -> RedirectResponse:
    user_id = os_billing.verify(t, kind)
    url = os_billing.page("expired")
    if user_id:
        try:
            url = await make(user_id)
        except stripe.StripeError as e:
            logger.warning("spark %s failed for %s: %s", kind, user_id, type(e).__name__)
    return RedirectResponse(url, status_code=303)


@router.get("/checkout", include_in_schema=False)
async def open_checkout(t: str = "") -> RedirectResponse:
    return await _open(os_billing.CHECKOUT, t, os_billing.checkout)


@router.get("/portal", include_in_schema=False)
async def open_portal(t: str = "") -> RedirectResponse:
    return await _open(os_billing.PORTAL, t, os_billing.portal)
