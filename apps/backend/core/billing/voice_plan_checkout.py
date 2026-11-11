"""Voice-plan Checkout: a normal Stripe subscription for the $250 flat fee (promotion codes allowed)."""

import time

import stripe

from core.billing import voice_plan
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import billing_repo

PURPOSE = "voice_plan"


async def start(owner_id: str, *, accepted_by: str) -> dict:
    if not voice_plan.available():
        raise voice_plan.VoicePlanError("voice_plan_unavailable")
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not account or not account.get("stripe_customer_id"):
        from core.services.billing_service import BillingService

        await BillingService().create_customer_for_owner(
            owner_id=owner_id, owner_type="org" if owner_id.startswith("org_") else "personal"
        )
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not account or not account.get("stripe_customer_id"):
        raise voice_plan.VoicePlanError("payment_setup_required")
    state = voice_plan.status(account)
    if state == "canceled":
        raise voice_plan.VoicePlanError("voice_plan_ended")
    if state != "not_enrolled":
        return {"status": state, "planVersion": voice_plan.PLAN_VERSION}
    metadata = {
        "purpose": PURPOSE,
        "workspace_id": owner_id,
        "accepted_by": accepted_by,
        "plan_version": voice_plan.PLAN_VERSION,
    }
    customer, key = account["stripe_customer_id"], settings.STRIPE_SECRET_KEY
    # One payable door per workspace: a paid-but-unsynced subscription or an open session is reused, never doubled.
    subs = await run_in_thread(
        stripe.Subscription.list, customer=customer, price=settings.STRIPE_VOICE_PLAN_PRICE_ID, limit=10, api_key=key
    )
    if any((s.get("metadata") or {}).get("workspace_id") == owner_id for s in subs["data"]):
        return {"status": "processing", "planVersion": voice_plan.PLAN_VERSION}
    sessions = await run_in_thread(
        stripe.checkout.Session.list, customer=customer, status="open", limit=20, api_key=key
    )
    for s in sessions["data"]:
        if (s.get("metadata") or {}).get("purpose") == PURPOSE and (s.get("metadata") or {}).get(
            "workspace_id"
        ) == owner_id:
            return {"status": "checkout", "checkoutUrl": s["url"]}
    session = await run_in_thread(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=account["stripe_customer_id"],
        line_items=[{"price": settings.STRIPE_VOICE_PLAN_PRICE_ID, "quantity": 1}],
        allow_promotion_codes=True,
        # A 100%-off code owes nothing: don't ask for a card.
        payment_method_collection="if_required",
        success_url=f"{settings.FRONTEND_URL}/voice/agent?voice=checkout_complete",  # shows it go live
        cancel_url=f"{settings.FRONTEND_URL}/voice/settings?voice=checkout_canceled",
        client_reference_id=owner_id,
        metadata=metadata,
        subscription_data={"metadata": metadata},
        idempotency_key=f"voice-sub:{owner_id}:{int(time.time() // 300)}",
        api_key=settings.STRIPE_SECRET_KEY,
    )
    return {"status": "checkout", "checkoutUrl": session["url"]}
