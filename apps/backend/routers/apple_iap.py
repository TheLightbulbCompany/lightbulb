"""Apple In-App Purchase — link endpoint and App Store Server Notifications V2.

Two routes with deliberately different auth models:

- ``POST /billing/apple/link`` is called by the signed-in app right after
  StoreKit reports a purchase. Clerk-authenticated; the owner comes from the
  token, NEVER from the body, so one user cannot link a purchase onto another
  account by editing the request.

- ``POST /webhooks/apple-iap`` is called by Apple, which cannot present a Clerk
  token. Its authentication IS the JWS signature on the payload — verified
  against Apple's root CA in ``apple_iap`` before anything is read out of it.
  There is no shared secret to check and none is needed; an attacker who could
  forge that signature could mint Apple's certificates.

See core/services/apple_iap.py for why verification is delegated to Apple's
library rather than hand-rolled.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import AuthContext, get_current_user, is_org_owner_id
from core.repositories import billing_repo
from core.services.apple_iap import AppleIapError, AppleIapNotConfigured, apply_notification, link_purchase
from core.services.billing_service import BillingService, BillingServiceError, create_mobile_web_checkout
from core.services.provision_gate import is_trial_blocked

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/billing/apple/link",
    summary="Link a completed Apple IAP purchase to the signed-in owner",
    description=(
        "Verifies a StoreKit 2 signed transaction against Apple's root CA and, "
        "on success, records the subscription on the caller's billing account. "
        "Idempotent — replaying the same transaction is safe."
    ),
)
async def link_apple_purchase(
    payload: dict,
    auth: AuthContext = Depends(get_current_user),
):
    signed_transaction = (payload or {}).get("signed_transaction")
    if not isinstance(signed_transaction, str) or not signed_transaction:
        raise HTTPException(status_code=400, detail="signed_transaction is required")

    try:
        subscription = await link_purchase(
            owner_id=auth.owner_id,
            signed_transaction=signed_transaction,
        )
    except AppleIapNotConfigured as e:
        # The Paid Apps Agreement isn't active / certs aren't wired. A 503 says
        # "not now", which the app can render honestly, rather than a 500.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AppleIapError as e:
        logger.warning("apple_iap.link_rejected owner_id=%s: %s", auth.owner_id, e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "status": subscription.status,
        "product_id": subscription.product_id,
        "expires_at": subscription.expires_at_ms,
    }


@router.post(
    "/billing/apple/web-checkout",
    summary="Create an app-to-web Stripe Checkout session for the iOS paywall",
    description=(
        "Returns a Stripe Checkout URL optimized for app-to-web purchases. The "
        "app must open it in the DEFAULT BROWSER — never a WebView, which would "
        "be an in-app storefront under guideline 3.1.1."
    ),
)
async def create_web_checkout(auth: AuthContext = Depends(get_current_user)):
    # Same fence as link_purchase, for the same reason: this session is built on
    # STRIPE_FLAT_PRICE_ID, the PERSONAL plan. An org billed through it would
    # get a single flat subscription where the rest of the system expects base +
    # per-seat — no subscription_item_id, so is_seat_subscription reads false
    # and sync_team_seat_quantity has nothing to drive. Checked BEFORE the
    # customer is minted so a wrong caller leaves no Stripe residue behind.
    if is_org_owner_id(auth.owner_id):
        raise HTTPException(
            status_code=403,
            detail="Team workspaces are billed per seat — subscribe from the web app.",
        )

    # Mint or reuse the Stripe customer first. The caller is by definition
    # someone who has not paid yet, and a trial_local row carries no
    # stripe_customer_id — without this the session build raises and every
    # free-tier user gets a 400 on the one button meant to convert them.
    try:
        await BillingService().create_customer_for_owner(
            auth.owner_id,
            owner_type="personal",
            email=auth.email,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apple_iap.web_checkout_customer_failed owner_id=%s: %s", auth.owner_id, e)
        raise HTTPException(status_code=502, detail="Could not reach billing provider") from e

    # Same guard the web checkout uses: an owner who already has (or ever had)
    # a subscription must not be able to mint a second one from the app.
    account = await billing_repo.get_by_owner_id(auth.owner_id)
    if is_trial_blocked(account):
        raise HTTPException(status_code=409, detail="This account already has a subscription")

    try:
        session = await create_mobile_web_checkout(owner_id=auth.owner_id)
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"url": session.url}


@router.post(
    "/webhooks/apple-iap",
    summary="App Store Server Notifications V2 receiver",
    description=(
        "Receives renewal, expiry, refund and billing-retry notifications. "
        "Authentication is the JWS signature on the payload itself. Deduped by "
        "Apple's notificationUUID via webhook_dedup."
    ),
)
async def apple_notifications(request: Request):
    body = await request.json()
    signed_payload = body.get("signedPayload") if isinstance(body, dict) else None
    if not isinstance(signed_payload, str) or not signed_payload:
        raise HTTPException(status_code=400, detail="signedPayload is required")

    # Dedup and the out-of-order guard both live inside apply_notification,
    # after signature verification — the notificationUUID and signedDate they
    # key on are only trustworthy once the payload is verified. See that
    # function for why reading them from the envelope would be exploitable.
    try:
        result = await apply_notification(signed_payload)
    except AppleIapNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AppleIapError as e:
        # 400 makes Apple retry. A verification failure here is either an
        # attack (retrying costs us nothing) or a misconfiguration on our side
        # that we want retried once fixed — losing a renewal silently would
        # cut off a paying customer at their paid-through date.
        logger.warning("apple_iap.notification_rejected: %s", e)
        raise HTTPException(status_code=400, detail="notification verification failed") from e

    if result is None:
        return {"received": True, "applied": False}

    owner_id, status = result
    return {"received": True, "applied": True, "owner_id": owner_id, "status": status}
