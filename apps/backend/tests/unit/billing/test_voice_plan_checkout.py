from unittest.mock import AsyncMock, MagicMock

import pytest

from core.billing import voice_plan_checkout as mod


@pytest.fixture
def env(monkeypatch):
    account = {
        "owner_id": "org",
        "stripe_customer_id": "cus",
        "voice_plan_checkout_intent": "intent",
        "voice_plan_checkout_expires_at": 4102444800,
        "voice_plan_accepted_by": "admin",
        "voice_plan_accepted_version": "voice-agents-v1",
    }
    monkeypatch.setattr(mod.voice_plan, "available", lambda: True)
    monkeypatch.setattr(mod.voice_plan.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="token"))
    monkeypatch.setattr(mod.voice_plan.container_repo, "release_owner_lifecycle_claim", AsyncMock())
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))
    monkeypatch.setattr(mod.billing_repo, "list_by_stripe_customer_id", AsyncMock(return_value=[account]))
    save = AsyncMock()
    monkeypatch.setattr(mod.billing_repo, "set_voice_checkout_intent", save)
    monkeypatch.setattr(mod.voice_plan_payments, "default_card", AsyncMock(return_value=False))
    monkeypatch.setattr(
        mod.stripe.Customer, "retrieve", MagicMock(return_value={"id": "cus", "metadata": {"workspace_id": "org"}})
    )
    checkout = MagicMock(return_value={"id": "cs", "url": "https://checkout.stripe.com/owned"})
    monkeypatch.setattr(mod.stripe.checkout.Session, "create", checkout)
    enroll = AsyncMock(return_value={"status": "pending_payment"})
    monkeypatch.setattr(mod.voice_plan, "_enrollment", enroll)
    monkeypatch.setattr(
        mod.stripe.SetupIntent,
        "retrieve",
        MagicMock(return_value={"status": "succeeded", "customer": "cus", "payment_method": "pm"}),
    )
    monkeypatch.setattr(
        mod.stripe.PaymentMethod, "retrieve", MagicMock(return_value={"customer": "cus", "type": "card"})
    )
    modify = MagicMock()
    monkeypatch.setattr(mod.stripe.Customer, "modify", modify)
    return account, checkout, enroll, modify, save


async def test_checkout_collects_a_card_without_creating_any_recurring_stripe_subscription(env):
    _, checkout, enroll, _, _ = env
    result = await mod.start("org", accepted_by="admin")
    assert result["status"] == "checkout"
    body = checkout.call_args.kwargs
    assert body["mode"] == "setup" and body["customer"] == "cus"
    assert "line_items" not in body and "subscription_data" not in body
    assert body["metadata"]["workspace_id"] == "org" and body["metadata"]["voice_intent"] == "intent"
    assert body["idempotency_key"] == "voice-setup:org:intent"
    enroll.assert_not_awaited()


async def test_shared_legacy_customer_cannot_have_default_card_changed_for_one_workspace(env, monkeypatch):
    _, checkout, enroll, modify, _ = env
    monkeypatch.setattr(mod.stripe.Customer, "retrieve", MagicMock(return_value={"id": "cus", "metadata": {}}))
    monkeypatch.setattr(
        mod.billing_repo,
        "list_by_stripe_customer_id",
        AsyncMock(return_value=[{"owner_id": "org"}, {"owner_id": "other"}]),
    )
    with pytest.raises(mod.voice_plan.VoicePlanError, match="payment_customer_shared"):
        await mod.start("org", accepted_by="admin")
    checkout.assert_not_called()
    enroll.assert_not_awaited()
    modify.assert_not_called()


def _event(**changes):
    session = {
        "mode": "setup",
        "status": "complete",
        "customer": "cus",
        "setup_intent": "seti",
        "metadata": {
            "purpose": mod.PURPOSE,
            "workspace_id": "org",
            "voice_intent": "intent",
            "plan_version": "voice-agents-v1",
        },
    }
    session.update(changes)
    return {"created": 1000, "data": {"object": session}}


async def test_only_completed_owned_setup_intent_can_set_card_and_continue_accepted_enrollment(env):
    _, _, enroll, modify, _ = env
    assert await mod.complete(_event())
    modify.assert_called_once()
    assert modify.call_args.args == ("cus",)
    assert modify.call_args.kwargs["invoice_settings"] == {"default_payment_method": "pm"}
    enroll.assert_awaited_once_with("org")


@pytest.mark.parametrize(
    "changes",
    [
        {"customer": "other"},
        {"status": "open"},
        {"mode": "subscription"},
        {
            "metadata": {
                "purpose": mod.PURPOSE,
                "workspace_id": "org",
                "voice_intent": "stale",
                "plan_version": "voice-agents-v1",
            }
        },
    ],
)
async def test_replayed_wrong_customer_or_unsettled_setup_cannot_enroll(env, changes):
    _, _, enroll, modify, _ = env
    assert await mod.complete(_event(**changes))
    modify.assert_not_called()
    enroll.assert_not_awaited()


async def test_ordinary_workspace_checkout_never_opts_into_voice(env):
    _, _, enroll, modify, _ = env
    assert not await mod.complete(_event(metadata={"workspace_id": "org"}))
    modify.assert_not_called()
    enroll.assert_not_awaited()


async def test_expired_intent_cannot_enroll_but_late_delivery_of_preexpiry_completion_can(env):
    account, _, enroll, _, _ = env
    account["voice_plan_checkout_expires_at"] = 500
    assert await mod.complete(_event())
    enroll.assert_not_awaited()
    event = _event()
    event["created"] = 400
    assert await mod.complete(event)
    enroll.assert_awaited_once()
