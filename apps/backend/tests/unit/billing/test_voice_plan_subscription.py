"""The $250 flat fee is a Stripe subscription; its status is the entitlement."""

from unittest.mock import AsyncMock, patch

import pytest

from core.billing import voice_billing, voice_plan, voice_plan_checkout
from core.config import settings

pytestmark = pytest.mark.asyncio

OWNER = "org_1"


def _sub(status="active", **extra):
    return {
        "id": "sub_1",
        "customer": "cus_1",
        "status": status,
        "metadata": {"purpose": "voice_plan", "workspace_id": OWNER, "accepted_by": "user_1"},
        "items": {"data": [{"price": {"id": "price_voice"}}]},
        # a 100%-off promotion code: nothing was charged, the subscription is still active
        "discount": {"coupon": {"percent_off": 100}},
        **extra,
    }


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for k, v in {
        "STRIPE_VOICE_PLAN_PRICE_ID": "price_voice",
        "METRONOME_VOICE_ENROLLMENT_ENABLED": True,
        "METRONOME_API_KEY": "m",
        "STRIPE_SECRET_KEY": "sk",
        "METRONOME_VOICE_RATE_CARD_ID": "card",
        "METRONOME_VOICE_METRIC_ID": "metric",
    }.items():
        monkeypatch.setattr(settings, k, v)


async def test_zero_dollar_subscription_grants_access_and_enrolls_usage_contract():
    account = {"owner_id": OWNER, "stripe_customer_id": "cus_1"}
    enrolled = {}

    async def enroll(owner_id, acct, client):
        enrolled["called"] = True
        account.update(metronome_voice_contract_id="contract_1", voice_plan_status="active")

    async def set_sub(owner_id, *, subscription_id, status, ending_at, accepted_by):
        account.update(voice_plan_stripe_subscription_id=subscription_id, voice_plan_stripe_status=status)

    with (
        patch.object(voice_plan.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda *a, **k: dict(account))),
        patch.object(voice_plan.billing_repo, "set_voice_subscription", set_sub),
        patch.object(voice_plan, "_enroll", enroll),
        patch.object(voice_plan, "_operation") as op,
        patch.object(voice_plan.voice_plan_payments, "refresh", AsyncMock()),
        patch.object(voice_plan, "run_in_thread", AsyncMock(return_value=_sub())),
    ):
        op.return_value.__aenter__ = AsyncMock()
        op.return_value.__aexit__ = AsyncMock(return_value=False)
        # A stale "incomplete" event arriving late still mirrors Stripe's current (active) state.
        await voice_plan.sync_subscription(_sub("incomplete"))
    assert enrolled and voice_billing.has_voice_plan(account)
    assert voice_plan.status(account) == "active"


async def test_unpaid_or_canceled_subscription_has_no_access():
    base = {"metronome_voice_contract_id": "c", "voice_plan_stripe_subscription_id": "sub_1"}
    assert not voice_billing.has_voice_plan({**base, "voice_plan_stripe_status": "past_due"})
    assert not voice_billing.has_voice_plan({**base, "voice_plan_stripe_status": "incomplete"})
    assert not voice_billing.has_voice_plan(
        {**base, "voice_plan_stripe_status": "active", "voice_plan_status": "canceled"}
    )


def _stripe(subs=(), sessions=()):
    async def run(fn, *a, **k):
        name = getattr(fn, "__qualname__", "")
        if "Subscription" in name:
            return {"data": list(subs)}
        if k.get("status") == "open":
            return {"data": list(sessions)}
        return {"url": "https://checkout/x"}

    return AsyncMock(side_effect=run)


async def _start(run):
    account = {"owner_id": OWNER, "stripe_customer_id": "cus_1"}
    with (
        patch.object(voice_plan_checkout.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan_checkout, "run_in_thread", run),
    ):
        return await voice_plan_checkout.start(OWNER, accepted_by="user_1")


async def test_checkout_is_a_subscription_with_promotion_codes():
    run = _stripe()
    out = await _start(run)
    kwargs = run.await_args.kwargs
    assert out["checkoutUrl"] == "https://checkout/x"
    assert kwargs["mode"] == "subscription" and kwargs["allow_promotion_codes"] is True
    assert kwargs["payment_method_collection"] == "if_required"
    assert kwargs["line_items"] == [{"price": "price_voice", "quantity": 1}]
    assert kwargs["subscription_data"]["metadata"]["workspace_id"] == OWNER


async def test_checkout_never_opens_a_second_payable_door():
    meta = {"purpose": "voice_plan", "workspace_id": OWNER}
    reused = await _start(_stripe(sessions=[{"url": "https://checkout/open", "metadata": meta}]))
    assert reused["checkoutUrl"] == "https://checkout/open"
    paid = await _start(_stripe(subs=[{"id": "sub_1"}]))
    assert paid["status"] == "processing" and "checkoutUrl" not in paid


async def test_voice_subscription_is_recognised_by_price():
    assert voice_plan.is_voice_subscription(_sub())
    assert not voice_plan.is_voice_subscription({"items": {"data": [{"price": {"id": "price_ws"}}]}})
