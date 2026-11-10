"""Explicit voice-plan enrollment. Never invoked by workspace provisioning or existing subscriptions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import httpx
import stripe

from core.billing import metronome_client, metronome_provisioning, voice_billing, voice_plan_payments
from core.dynamodb import run_in_thread
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
        and settings.STRIPE_VOICE_PLAN_PRICE_ID
        and settings.METRONOME_VOICE_RATE_CARD_ID
        and settings.METRONOME_VOICE_METRIC_ID
    )


def is_voice_subscription(subscription: dict) -> bool:
    price_id = settings.STRIPE_VOICE_PLAN_PRICE_ID
    return bool(price_id) and any(
        ((item or {}).get("price") or {}).get("id") == price_id
        for item in (subscription.get("items") or {}).get("data") or []
    )


def status(account: dict) -> str:
    if not account.get("voice_plan_stripe_subscription_id"):
        return "not_enrolled"
    if account.get("voice_plan_status") == "canceled" or account.get("voice_plan_stripe_status") in (
        "canceled",
        "incomplete_expired",
    ):
        return "canceled"
    if voice_billing.has_voice_plan(account):
        return "canceling" if account.get("voice_plan_ending_at") else "active"
    return "past_due" if account.get("voice_plan_stripe_status") in ("past_due", "unpaid") else "pending_payment"


async def sync_subscription(subscription: dict) -> None:
    """Mirror the voice-plan Stripe subscription (created/updated/deleted); enroll or end the usage contract."""
    metadata = subscription.get("metadata") or {}
    owner_id = metadata.get("workspace_id")
    if not owner_id or metadata.get("purpose") != "voice_plan":
        return
    async with _operation(owner_id):
        # Webhooks arrive out of order: mirror Stripe's current state, never the event's snapshot.
        subscription = await run_in_thread(
            stripe.Subscription.retrieve, subscription["id"], api_key=settings.STRIPE_SECRET_KEY
        )
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if not account or account.get("stripe_customer_id") != subscription.get("customer"):
            return
        cancel_at = subscription.get("cancel_at") or (
            subscription.get("canceled_at") if subscription["status"] == "canceled" else None
        )
        ending_at = datetime.fromtimestamp(cancel_at, UTC).isoformat() if cancel_at else None
        await billing_repo.set_voice_subscription(
            owner_id,
            subscription_id=subscription["id"],
            status=subscription["status"],
            ending_at=ending_at,
            accepted_by=metadata.get("accepted_by"),
        )
        if subscription["status"] in ("active", "trialing") and not account.get("metronome_voice_contract_id"):
            if not available():
                raise VoicePlanError("voice_plan_unavailable")  # 500s the webhook so Stripe redelivers
            async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
                await _enroll(owner_id, account, metronome_client.for_settings(http))
        elif subscription["status"] == "canceled" and account.get("metronome_voice_contract_id"):
            await _end(owner_id, account, ending_at or datetime.now(UTC).isoformat())
        await voice_plan_payments.refresh(owner_id)


async def end_for_reversal(subscription_id: str) -> str | None:
    """A refunded or disputed voice-plan payment ends the plan now: cancel (once) and mirror it, which
    ends the usage contract. Returns the workspace id, or None when the subscription is not a voice plan."""
    subscription = await run_in_thread(
        stripe.Subscription.retrieve, subscription_id, api_key=settings.STRIPE_SECRET_KEY
    )
    metadata = subscription.get("metadata") or {}
    owner_id = metadata.get("workspace_id")
    if not owner_id or metadata.get("purpose") != "voice_plan" or not is_voice_subscription(subscription):
        return None
    if subscription["status"] != "canceled":
        await run_in_thread(stripe.Subscription.cancel, subscription_id, api_key=settings.STRIPE_SECRET_KEY)
    await sync_subscription(subscription)
    return owner_id


async def announce_reversal(owner_id: str) -> None:
    """One Needs-you card per workspace human (rendered as a reconnect-kind item; see activity). Best-effort."""
    import time

    from core.dynamodb import utc_now_iso
    from core.repositories import messaging_repo
    from core.services import website_logins
    from core.services.activity import reconnect_item_id
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    created = utc_now_iso()
    for user_id in await website_logins._humans_to_ask(owner_id, None):
        await messaging_repo.put_activity_row(
            {
                "pk": messaging_repo.activity_pk(owner_id, user_id),
                "sk": messaging_repo.activity_sk(created, reconnect_item_id("voice_plan")),
                "kind": "reconnect",
                "provider": "voice_plan",
                "createdAt": created,
                "expiresAt": int(time.time()) + website_logins.ACTIVITY_TTL_S,
            }
        )
    await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"}))


async def _enroll(owner_id: str, account: dict, client) -> None:
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


def period(account: dict, at: datetime | None = None) -> tuple[datetime, datetime]:
    now = (at or datetime.now(UTC)).astimezone(UTC)
    ending_at = datetime.fromisoformat(account["voice_plan_ending_at"]) if account.get("voice_plan_ending_at") else None
    if ending_at and ending_at <= now:
        now = ending_at - timedelta(microseconds=1)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    if account.get("voice_plan_started_at"):
        start = max(start, datetime.fromisoformat(account["voice_plan_started_at"]))
    return start, min(end, ending_at) if ending_at else end


async def usage(account: dict, *, client=None, at: datetime | None = None) -> dict:
    if not settings.METRONOME_VOICE_METRIC_ID:
        raise VoicePlanError("voice_usage_unavailable")
    # A delayed, durable usage check must query the period that its original
    # voice event belongs to, even if a Temporal retry runs after UTC rollover.
    start, end = period(account, at=at)
    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10) as http:
            return await usage(account, client=metronome_client.for_settings(http), at=at)
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


async def cancel(owner_id: str) -> dict:
    """Stop renewal at period end; the subscription webhook then records the end date."""
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if not account.get("voice_plan_stripe_subscription_id") or status(account) == "canceled":
        return {"status": "canceled", "planVersion": PLAN_VERSION}
    sub = await run_in_thread(
        stripe.Subscription.modify,
        account["voice_plan_stripe_subscription_id"],
        cancel_at_period_end=True,
        api_key=settings.STRIPE_SECRET_KEY,
    )
    await sync_subscription(sub)
    return {
        "status": "canceling",
        "planVersion": PLAN_VERSION,
        "endsAt": (await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}).get(
            "voice_plan_ending_at"
        ),
    }


async def _end(owner_id: str, account: dict, ending_at: str) -> None:
    # Metronome ends contracts on a day boundary: the next UTC midnight covers any usage up to now.
    midnight = (datetime.now(UTC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
        await metronome_client.for_settings(http).end_voice_contract(
            customer_id=account["metronome_customer_id"],
            contract_id=account["metronome_voice_contract_id"],
            ending_before=midnight.isoformat(),
        )
    await billing_repo.end_voice_plan(owner_id, ending_at=ending_at)
