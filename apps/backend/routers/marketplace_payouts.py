"""Stripe Connect Express onboarding + dashboard endpoints."""

import asyncio
import logging
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user, require_org_admin
from core.config import settings
from core.repositories import marketplace_repo
from core.services import payout_service


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace/payouts", tags=["marketplace-payouts"])


@router.post("/onboard")
async def onboard(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Get an onboarding link. Creates a Connect account if absent.

    Guards against the create_connect_account 24h-idempotency-window trap:
    re-uses the seller's existing Stripe account_id from DDB if any, instead
    of relying on Stripe-side idempotency.
    """
    require_org_admin(auth)
    pa = await marketplace_repo.get_payout_account(auth.user_id) or {}
    account_id = pa.get("stripe_connect_account_id")
    if not account_id:
        account_id = await payout_service.create_connect_account(
            seller_id=auth.user_id, email=auth.email or "", country="US"
        )
        claimed = await marketplace_repo.set_connect_account_if_absent(seller_id=auth.user_id, account_id=account_id)
        if not claimed:
            # Another concurrent onboarding wrote first. Re-read to use
            # the winning account_id (Stripe tolerates orphan empty
            # accounts; the second create_connect_account is operational
            # cost we accept v0).
            pa = await marketplace_repo.get_payout_account(auth.user_id) or {}
            account_id = pa.get("stripe_connect_account_id") or account_id

    url = await payout_service.create_onboarding_link(
        connect_account_id=account_id,
        refresh_url=settings.STRIPE_CONNECT_REFRESH_URL,
        return_url=settings.STRIPE_CONNECT_RETURN_URL,
    )
    return {"onboarding_url": url}


@router.get("/dashboard")
async def dashboard(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Stripe-hosted dashboard link + held-balance snapshot."""
    require_org_admin(auth)
    pa = await marketplace_repo.get_payout_account(auth.user_id) or {}
    # Balance fields are sourced from DDB (not Stripe) so they always render,
    # even when the Connect account is missing or unreachable below.
    balance = {
        "balance_held_cents": pa.get("balance_held_cents", 0),
        "lifetime_earned_cents": pa.get("lifetime_earned_cents", 0),
    }
    account_id = pa.get("stripe_connect_account_id")
    if not account_id:
        return {"dashboard_url": None, "payouts_enabled": False, **balance}
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        # Login links resolve while payouts are still pending verification, so
        # dashboard_url alone is NOT "payable" — payouts_enabled is the real
        # readiness signal, read off the account itself. Blocking Stripe SDK
        # calls run on a worker thread.
        account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
        link = await asyncio.to_thread(stripe.Account.create_login_link, account_id)
    except stripe.error.StripeError:
        # Graceful degrade: a Connect account that hasn't finished onboarding
        # (or was deleted/invalidated) makes Stripe raise here. Returning the
        # balance with a null dashboard_url lets the earnings page render its
        # "finish payout setup" state instead of 500-ing the whole page.
        logger.warning(
            "Stripe login link unavailable for seller %s (account %s)",
            auth.user_id,
            account_id,
        )
        return {"dashboard_url": None, "payouts_enabled": False, **balance}
    return {
        "dashboard_url": link.url,
        "payouts_enabled": bool(account.get("payouts_enabled")),
        **balance,
    }
