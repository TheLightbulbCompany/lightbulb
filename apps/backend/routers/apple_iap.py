"""Apple In-App Purchase — link endpoint and App Store Server Notifications V2.

Two routes with deliberately different auth models:

- ``POST /billing/apple/purchase-intent`` and ``POST /billing/apple/link`` are
  Clerk-authenticated, org-admin-only. The intent route hands the app the
  ``appAccountToken`` a StoreKit purchase must carry (404 while
  ``APPLE_IAP_SELLING`` is off); the link route verifies the signed transaction
  and records the subscription on the workspace.

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

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo
from core.services import apple_iap as apple_iap_service
from core.services.apple_iap import (
    PRODUCT_PLANS,
    AppleIapError,
    AppleIapNotConfigured,
    CheckoutInProgress,
    StripeManaged,
    link_purchase,
    owner_account_token,
    stripe_is_live,
)
from core.services.billing_service import BillingServiceError, retire_pending_checkout_for_apple

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/billing/apple/purchase-intent",
    summary="Start an in-app purchase for this workspace",
    description="Admin only. Returns the appAccountToken the StoreKit purchase must carry.",
)
async def apple_purchase_intent(auth: AuthContext = Depends(get_current_user)):
    # Runs BEFORE StoreKit charges: every refusal here costs the customer nothing.
    require_org_admin(auth)
    if not settings.APPLE_IAP_SELLING:
        raise HTTPException(status_code=404, detail="In-app purchase is not available")
    owner_id = resolve_owner_id(auth)
    account = await billing_repo.get_by_owner_id(owner_id)
    if stripe_is_live(account):
        raise HTTPException(status_code=409, detail="stripe_managed")
    try:
        # An open web Checkout completing after this purchase would bill the workspace twice.
        await retire_pending_checkout_for_apple(owner_id, account)
    except BillingServiceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"app_account_token": owner_account_token(owner_id), "product_ids": list(PRODUCT_PLANS)}


@router.post(
    "/billing/apple/link",
    summary="Link a StoreKit purchase to this workspace",
    description="Admin only. Verifies the signed transaction and records the subscription. Idempotent.",
)
async def link_apple_purchase(payload: dict, auth: AuthContext = Depends(get_current_user)):
    # Deliberately NOT gated on APPLE_IAP_SELLING: a purchase Apple already charged
    # must always be linkable (restore, relaunch), even after selling is switched off.
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    signed_transaction = (payload or {}).get("signed_transaction")
    if not isinstance(signed_transaction, str) or not signed_transaction:
        raise HTTPException(status_code=400, detail="signed_transaction is required")
    try:
        subscription = await link_purchase(owner_id=owner_id, signed_transaction=signed_transaction)
    except AppleIapNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except StripeManaged as e:
        raise HTTPException(status_code=409, detail="stripe_managed") from e
    except CheckoutInProgress as e:
        # Transient: the transaction stays unfinished and StoreKit redelivers it.
        raise HTTPException(status_code=409, detail="checkout_in_progress") from e
    except AppleIapError as e:
        logger.warning("apple_iap.link_rejected owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "status": subscription.status,
        "product_id": subscription.product_id,
        "plan": subscription.plan,
        "expires_at": subscription.expires_at_ms,
    }


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
    # The body is parsed by hand rather than declared as a Pydantic model
    # because the payload is one opaque JWS string and the route must answer 400
    # (not 422) for anything malformed. That means owning the decode error too:
    # an empty or non-JSON POST would otherwise raise JSONDecodeError and return
    # 500. A 500 on a webhook is worse than a rejection — providers treat it as
    # "try again", so a permanently malformed delivery would be retried for days
    # against an endpoint that can never accept it.
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001 — any decode failure is a bad request
        raise HTTPException(status_code=400, detail="body must be JSON") from e

    signed_payload = body.get("signedPayload") if isinstance(body, dict) else None
    if not isinstance(signed_payload, str) or not signed_payload:
        raise HTTPException(status_code=400, detail="signedPayload is required")

    # Dedup and the out-of-order guard both live inside apply_notification,
    # after signature verification — the notificationUUID and signedDate they
    # key on are only trustworthy once the payload is verified. See that
    # function for why reading them from the envelope would be exploitable.
    try:
        result = await apple_iap_service.apply_notification(signed_payload)
    except apple_iap_service.AppleIapNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except apple_iap_service.AppleIapError as e:
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
