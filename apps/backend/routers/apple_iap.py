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

from core.auth import AuthContext, get_current_user, is_org_owner_id, require_org_admin, resolve_owner_id
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
    # AuthContext carries user_id / org_id, NOT owner_id — the container and
    # workspace owner is DERIVED (org_id in org context, else user_id), which is
    # what resolve_owner_id does. Reaching for `auth.owner_id` raises
    # AttributeError and 500s the endpoint before verification even starts;
    # every authenticated Apple billing route had that bug.
    owner_id = resolve_owner_id(auth)
    signed_transaction = (payload or {}).get("signed_transaction")
    if not isinstance(signed_transaction, str) or not signed_transaction:
        raise HTTPException(status_code=400, detail="signed_transaction is required")

    try:
        subscription = await link_purchase(
            owner_id=owner_id,
            signed_transaction=signed_transaction,
        )
    except AppleIapNotConfigured as e:
        # The Paid Apps Agreement isn't active / certs aren't wired. A 503 says
        # "not now", which the app can render honestly, rather than a 500.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AppleIapError as e:
        logger.warning("apple_iap.link_rejected owner_id=%s: %s", owner_id, e)
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
    owner_id = resolve_owner_id(auth)
    # Same fence as link_purchase, for the same reason: this session is built on
    # STRIPE_WORKSPACE_PRICE_ID. Legacy organization checkout is still handled
    # by the legacy web path until Task 2 replaces that route atomically.
    # Checked BEFORE the customer is minted so a wrong caller leaves no Stripe
    # residue behind.
    if is_org_owner_id(owner_id):
        raise HTTPException(
            status_code=403,
            detail="Workspace subscriptions must be started from the web app.",
        )

    # Mint or reuse the Stripe customer first. The caller is by definition
    # someone who has not paid yet, and a trial_local row carries no
    # stripe_customer_id — without this the session build raises and every
    # free-tier user gets a 400 on the one button meant to convert them.
    try:
        await BillingService().create_customer_for_owner(
            owner_id,
            owner_type="personal",
            email=auth.email,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apple_iap.web_checkout_customer_failed owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail="Could not reach billing provider") from e

    # Same guard the web checkout uses: an owner who already has (or ever had)
    # a subscription must not be able to mint a second one from the app.
    account = await billing_repo.get_by_owner_id(owner_id)
    if is_trial_blocked(account):
        raise HTTPException(status_code=409, detail="This account already has a subscription")

    try:
        session = await create_mobile_web_checkout(owner_id=owner_id)
    except BillingServiceError as e:
        # A held slot means another checkout is mid-flight for this owner —
        # a retryable conflict, not a bad request, and mapping it to 400 would
        # tell the app the request itself was wrong.
        if str(e) == "checkout_in_progress":
            raise HTTPException(status_code=409, detail="A checkout is already in progress") from e
        if str(e).startswith("already_subscribed:"):
            # The pending session is already PAID and its webhook is still in
            # flight, so is_trial_blocked above read a row that says nothing yet.
            # Handing back a second session here is how one owner gets charged
            # twice.
            raise HTTPException(status_code=409, detail="This account already has a subscription") from e
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"url": session.url}


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
    whatever subscription the owner already has, including a Teams per-seat one,
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
