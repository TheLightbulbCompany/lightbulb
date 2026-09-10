"""Apple In-App Purchase — link endpoint and App Store Server Notifications V2.

Two routes with deliberately different auth models:

- ``POST /billing/apple/link`` is a Clerk-authenticated tombstone for retired
  mobile purchase clients. It always returns 410 Gone and never verifies or
  records the submitted transaction.

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
from core.repositories import billing_repo
from core.services import apple_iap as apple_iap_service
from core.services.billing_service import BillingService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/billing/apple/link",
    summary="Retired Apple IAP purchase-link endpoint",
    description="Always returns 410 Gone. New StoreKit purchases are no longer accepted.",
)
async def link_apple_purchase(
    _auth: AuthContext = Depends(get_current_user),
):
    raise HTTPException(status_code=410, detail="Apple in-app purchases are no longer available")


@router.post(
    "/billing/apple/portal",
    summary="Stripe customer portal session that returns into the iOS app",
    description=(
        "Same portal as POST /billing/portal, but with a universal-link return "
        "URL so iOS hands control back to the app when the customer finishes."
    ),
)
async def create_mobile_portal(auth: AuthContext = Depends(get_current_user)):
    """Exists ONLY for the return URL.

    /billing/portal sends the customer to the web settings page when they are
    done, which is correct in a browser and wrong from the app — a subscriber
    who just cancelled would be left standing on isol8.co in Safari instead of
    back where they started. Stripe's iOS guidance is to point `return_url` at
    a universal link, which is what this does; the path is covered by the same
    apple-app-site-association entry as the checkout return.

    The path is chosen HERE, not accepted from the client: a caller-supplied
    return_url on a billing endpoint is an open redirect.

    Unlike the checkout endpoints there is no org-OWNER fence: this manages
    whatever subscription the owner already has, including a Workspace or Ultra one,
    and changes no plan shape — so an org admin is a legitimate caller.
    An org MEMBER is not. `resolve_owner_id` returns the ORG's billing row in
    org context, so without the admin check any member could open a portal
    session that cancels the whole team's subscription. `/billing/portal` has
    carried this check all along; the mobile return-to-app twin was written from
    its behaviour and inherited the reasoning without the guard.
    """
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="Billing account not found")

    try:
        url = await BillingService().create_portal_session(account, return_path="/checkout_redirect/portal")
    except Exception as e:  # noqa: BLE001
        logger.warning("apple_iap.portal_failed owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Could not reach billing provider") from e

    return {"portal_url": url}


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
