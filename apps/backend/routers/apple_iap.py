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

from core.auth import AuthContext, get_current_user
from core.services.apple_iap import AppleIapError, AppleIapNotConfigured, apply_notification, link_purchase

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
