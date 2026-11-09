"""Explicit voice-plan enrollment. Never invoked by workspace provisioning or existing subscriptions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import time
import uuid

import httpx

from core.billing import metronome_client, metronome_provisioning, voice_billing, voice_plan_payments
from core.config import settings
from core.repositories import billing_repo, container_repo

PLAN_VERSION = "voice-agents-v1"


class VoicePlanError(Exception):
    pass


@asynccontextmanager
async def _operation(owner_id: str):
    key = f"voice-plan#{owner_id}"
    token = await container_repo.try_claim_owner_lifecycle(key, stale_s=180)
    if not token:
        raise VoicePlanError("voice_plan_busy")
    try:
        yield
    finally:
        await container_repo.release_owner_lifecycle_claim(key, token=token)


def available() -> bool:
    return bool(
        settings.METRONOME_VOICE_ENROLLMENT_ENABLED
        and settings.METRONOME_API_KEY
        and settings.STRIPE_SECRET_KEY
        and settings.METRONOME_VOICE_RATE_CARD_ID
        and settings.METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID
        and settings.METRONOME_VOICE_METRIC_ID
    )


def status(account: dict) -> str:
    if not account.get("metronome_voice_contract_id"):
        return "not_enrolled"
    if account.get("voice_plan_status") == "canceled":
        return "canceled"
    if account.get("voice_plan_cancel_target") and not account.get("voice_plan_ending_at"):
        return "cancellation_pending"
    if account.get("voice_plan_ending_at") and datetime.fromisoformat(account["voice_plan_ending_at"]) <= datetime.now(
        UTC
    ):
        return "canceled"
    if voice_billing.has_voice_plan(account):
        return "canceling" if account.get("voice_plan_ending_at") else "active"
    return "past_due" if account.get("voice_plan_unpaid_invoice_ids") else "pending_payment"


async def enroll(owner_id: str, *, accepted_by: str | None = None, client=None) -> dict:
    """Create/adopt exactly one voice contract after an admin accepts this plan's terms.

    IDs persist last. A crash or concurrent enrollment reuses the deterministic provider key;
    it cannot create a second subscription. No existing credit contract is changed.
    """
    async with _operation(owner_id):
        if accepted_by:
            if not available():
                raise VoicePlanError("voice_plan_unavailable")
            account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
            if not account.get("stripe_customer_id"):
                raise VoicePlanError("payment_setup_required")
            if not account.get("metronome_voice_contract_id") and (
                account.get("voice_plan_accepted_version") != PLAN_VERSION
                or int(account.get("voice_plan_checkout_expires_at") or 0) <= time.time() + 60
            ):
                await billing_repo.set_voice_checkout_intent(
                    owner_id, intent=str(uuid.uuid4()), expires_at=int(time.time()) + 3600, accepted_by=accepted_by
                )
        return await _enrollment(owner_id, client=client)


async def _enrollment(owner_id: str, *, client=None) -> dict:
    if not available():
        raise VoicePlanError("voice_plan_unavailable")
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not account or not account.get("stripe_customer_id"):
        raise VoicePlanError("payment_setup_required")
    if account.get("metronome_voice_contract_id"):
        if status(account) == "canceled":
            raise VoicePlanError("voice_plan_ended")
        account = await voice_plan_payments.reconcile(owner_id, client=client)
        return {"status": status(account), "planVersion": PLAN_VERSION}
    if account.get("voice_plan_accepted_version") != PLAN_VERSION or not account.get("voice_plan_accepted_by"):
        raise VoicePlanError("voice_plan_acceptance_required")
    if not await voice_plan_payments.default_card(account["stripe_customer_id"]):
        raise VoicePlanError("payment_setup_required")
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
            return await _enroll(owner_id, account, metronome_client.for_settings(http))
    return await _enroll(owner_id, account, client)


async def _enroll(owner_id: str, account: dict, client) -> dict:
    customer_id = account.get("metronome_customer_id") or await metronome_provisioning._create_or_adopt_customer(
        owner_id, client
    )
    if not await metronome_provisioning._maybe_link_stripe(owner_id, account, customer_id, client):
        raise VoicePlanError("payment_setup_required")
    key = f"lightbulb-voice:{customer_id}:{PLAN_VERSION}"
    existing = next(
        (c for c in await client.list_contracts(customer_id=customer_id) if c.get("uniqueness_key") == key), None
    )
    if existing:
        contract_id = existing["id"]
    else:
        try:
            contract_id = await client.create_voice_contract(
                customer_id=customer_id,
                rate_card_id=settings.METRONOME_VOICE_RATE_CARD_ID,
                subscription_product_id=settings.METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID,
                uniqueness_key=key,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            winner = next(
                (c for c in await client.list_contracts(customer_id=customer_id) if c.get("uniqueness_key") == key),
                None,
            )
            if not winner:
                raise
            contract_id = winner["id"]
    contract = await client.get_contract(customer_id=customer_id, contract_id=contract_id)
    contract = contract.get("contract") or contract
    started_at = contract.get("starting_at")
    if not started_at:
        raise VoicePlanError("voice_contract_incomplete")
    await billing_repo.set_voice_plan(
        owner_id, customer_id=customer_id, contract_id=contract_id, usage_key=key, started_at=started_at
    )
    account = await voice_plan_payments.reconcile(owner_id, client=client)
    return {"status": status(account), "planVersion": PLAN_VERSION}


def period(account: dict, at: datetime | None = None) -> tuple[datetime, datetime]:
    now = (at or datetime.now(UTC)).astimezone(UTC)
    ending_at = datetime.fromisoformat(account["voice_plan_ending_at"]) if account.get("voice_plan_ending_at") else None
    if ending_at and ending_at <= now:
        from datetime import timedelta

        now = ending_at - timedelta(microseconds=1)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    if account.get("voice_plan_started_at"):
        start = max(start, datetime.fromisoformat(account["voice_plan_started_at"]))
    return start, min(end, ending_at) if ending_at else end


async def usage(account: dict, *, client=None) -> dict:
    if not settings.METRONOME_VOICE_METRIC_ID:
        raise VoicePlanError("voice_usage_unavailable")
    start, end = period(account)
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10) as http:
            return await usage(account, client=metronome_client.for_settings(http))
    seconds = await client.scoped_voice_seconds(
        customer_id=account["metronome_customer_id"],
        metric_id=settings.METRONOME_VOICE_METRIC_ID,
        usage_key=account["voice_plan_usage_key"],
        starting_on=voice_plan_payments.utc(start.isoformat()),
        ending_before=voice_plan_payments.utc(end.isoformat()),
    )
    return {
        "used": seconds / 60,
        "usedSeconds": seconds,
        "periodStart": start.isoformat(),
        "periodEnd": end.isoformat(),
        "timeZone": "UTC",
        "included": voice_billing.INCLUDED_MINUTES,
        "overageRate": voice_billing.OVERAGE_USD_PER_MINUTE,
    }


async def cancel(owner_id: str, *, client=None) -> dict:
    """Cancellation remains available even while new enrollments are switched off."""
    async with _operation(owner_id):
        return await _cancellation(owner_id, client=client)


async def _cancellation(owner_id: str, *, client=None) -> dict:
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if not account.get("metronome_voice_contract_id") or account.get("voice_plan_status") == "canceled":
        return {"status": "canceled", "planVersion": PLAN_VERSION}
    if account.get("voice_plan_status") == "canceling":
        return {"status": "canceling", "planVersion": PLAN_VERSION, "endsAt": account["voice_plan_ending_at"]}
    ending_at = await billing_repo.begin_voice_plan_cancellation(owner_id)
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
            return await _cancel(owner_id, account, ending_at, metronome_client.for_settings(http))
    return await _cancel(owner_id, account, ending_at, client)


async def _cancel(owner_id: str, account: dict, ending_at: str, client) -> dict:
    await client.end_voice_contract(
        customer_id=account["metronome_customer_id"],
        contract_id=account["metronome_voice_contract_id"],
        ending_before=ending_at,
    )
    await billing_repo.finish_voice_plan_cancellation(owner_id)
    await voice_plan_payments.refresh(owner_id)
    return {"status": "canceling", "planVersion": PLAN_VERSION, "endsAt": ending_at}
