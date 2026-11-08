"""Explicit voice-plan enrollment. Never invoked by workspace provisioning or existing subscriptions."""

from contextlib import asynccontextmanager

import httpx

from core.billing import metronome_client, metronome_provisioning, voice_billing
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


async def enroll(owner_id: str, *, client=None) -> dict:
    """Create/adopt exactly one voice contract after an admin accepts this plan's terms.

    IDs persist last. A crash or concurrent enrollment reuses the deterministic provider key;
    it cannot create a second subscription. No existing credit contract is changed.
    """
    async with _operation(owner_id):
        return await _enrollment(owner_id, client=client)


async def _enrollment(owner_id: str, *, client=None) -> dict:
    if not (
        settings.METRONOME_VOICE_ENROLLMENT_ENABLED
        and settings.METRONOME_API_KEY
        and settings.METRONOME_VOICE_RATE_CARD_ID
        and settings.METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID
    ):
        raise VoicePlanError("voice_plan_unavailable")
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not account or not account.get("stripe_customer_id"):
        raise VoicePlanError("payment_setup_required")
    if voice_billing.has_voice_plan(account):
        return {"status": account["voice_plan_status"], "planVersion": PLAN_VERSION}
    if account.get("metronome_voice_contract_id"):
        raise VoicePlanError("voice_plan_ended")
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
    await billing_repo.set_voice_plan(owner_id, customer_id=customer_id, contract_id=contract_id, usage_key=key)
    return {"status": "active", "planVersion": PLAN_VERSION}


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
    return {"status": "canceling", "planVersion": PLAN_VERSION, "endsAt": ending_at}
