"""Owner-only card setup and accepted voice enrollment; no ordinary checkout enrolls a workspace."""

import time
import uuid

import stripe

from core.billing import voice_plan, voice_plan_payments
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import billing_repo

PURPOSE = "voice_plan_setup"


async def start(owner_id: str, *, accepted_by: str) -> dict:
    if not voice_plan.available():
        raise voice_plan.VoicePlanError("voice_plan_unavailable")
    async with voice_plan._operation(owner_id):
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        if not account or not account.get("stripe_customer_id"):
            from core.services.billing_service import BillingService

            await BillingService().create_customer_for_owner(
                owner_id=owner_id, owner_type="org" if owner_id.startswith("org_") else "personal"
            )
            account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        if not account or not account.get("stripe_customer_id"):
            raise voice_plan.VoicePlanError("payment_setup_required")
        if account.get("metronome_voice_contract_id"):
            return await voice_plan._enrollment(owner_id)
        customer_id = account["stripe_customer_id"]
        ready = await voice_plan_payments.default_card(customer_id)
        if not ready:
            # A customer shared by old workspaces cannot have its default card changed for this workspace.
            customer = await run_in_thread(stripe.Customer.retrieve, customer_id, api_key=settings.STRIPE_SECRET_KEY)
            owner = (customer.get("metadata") or {}).get("workspace_id")
            rows = await billing_repo.list_by_stripe_customer_id(customer_id)
            if owner != owner_id and not (not owner and len(rows) == 1 and rows[0].get("owner_id") == owner_id):
                raise voice_plan.VoicePlanError("payment_customer_shared")
        expires = int(account.get("voice_plan_checkout_expires_at") or 0)
        intent = account.get("voice_plan_checkout_intent") if expires > time.time() + 60 else None
        if not intent:
            intent, expires = str(uuid.uuid4()), int(time.time()) + 3600
            await billing_repo.set_voice_checkout_intent(
                owner_id, intent=intent, expires_at=expires, accepted_by=accepted_by
            )
        if ready:
            return await voice_plan._enrollment(owner_id)
        session = await run_in_thread(
            stripe.checkout.Session.create,
            mode="setup",
            customer=customer_id,
            payment_method_types=["card"],
            success_url=f"{settings.FRONTEND_URL}/voice/settings?voice=setup_complete",
            cancel_url=f"{settings.FRONTEND_URL}/voice/settings?voice=setup_canceled",
            client_reference_id=owner_id,
            expires_at=expires,
            metadata={
                "purpose": PURPOSE,
                "workspace_id": owner_id,
                "voice_intent": intent,
                "plan_version": voice_plan.PLAN_VERSION,
            },
            setup_intent_data={"metadata": {"purpose": PURPOSE, "workspace_id": owner_id, "voice_intent": intent}},
            idempotency_key=f"voice-setup:{owner_id}:{intent}",
            api_key=settings.STRIPE_SECRET_KEY,
        )
        return {"status": "checkout", "checkoutUrl": session["url"]}


async def complete(event: dict) -> bool:
    session = event["data"]["object"]
    metadata = session.get("metadata") or {}
    if metadata.get("purpose") != PURPOSE:
        return False
    owner_id = metadata.get("workspace_id")
    if not owner_id or metadata.get("plan_version") != voice_plan.PLAN_VERSION:
        return True
    async with voice_plan._operation(owner_id):
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if not (
            session.get("mode") == "setup"
            and session.get("status") == "complete"
            and session.get("customer") == account.get("stripe_customer_id")
            and metadata.get("voice_intent") == account.get("voice_plan_checkout_intent")
            and account.get("voice_plan_accepted_version") == voice_plan.PLAN_VERSION
            and account.get("voice_plan_accepted_by")
            and int(event.get("created") or 0) <= int(account.get("voice_plan_checkout_expires_at") or 0)
        ):
            return True
        setup_id = session.get("setup_intent")
        if isinstance(setup_id, dict):
            setup_id = setup_id.get("id")
        setup = await run_in_thread(stripe.SetupIntent.retrieve, setup_id, api_key=settings.STRIPE_SECRET_KEY)
        if setup.get("status") != "succeeded" or setup.get("customer") != account["stripe_customer_id"]:
            return True
        method_id = setup.get("payment_method")
        if isinstance(method_id, dict):
            method_id = method_id.get("id")
        method = await run_in_thread(stripe.PaymentMethod.retrieve, method_id, api_key=settings.STRIPE_SECRET_KEY)
        if method.get("type") != "card" or method.get("customer") != account["stripe_customer_id"]:
            return True
        if not await voice_plan_payments.default_card(account["stripe_customer_id"]):
            await run_in_thread(
                stripe.Customer.modify,
                account["stripe_customer_id"],
                invoice_settings={"default_payment_method": method_id},
                api_key=settings.STRIPE_SECRET_KEY,
            )
        if voice_plan.available():
            await voice_plan._enrollment(owner_id)
    return True
