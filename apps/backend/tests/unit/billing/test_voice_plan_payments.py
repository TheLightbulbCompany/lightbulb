from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.billing import voice_plan_payments as mod

ACCOUNT = {
    "owner_id": "org",
    "metronome_customer_id": "mc",
    "metronome_voice_contract_id": "voice",
    "stripe_customer_id": "cus",
}
INVOICE = {
    "id": "mi",
    "customer_id": "mc",
    "contract_id": "voice",
    "total": 25000,
    "start_timestamp": "2026-11-01T00:00:00Z",
    "external_invoice": {"billing_provider_type": "stripe", "invoice_id": "in_fee"},
    "line_items": [
        {
            "product_id": "voice-sub",
            "type": "subscription",
            "quantity": 1,
            "starting_at": "2026-11-01T00:00:00Z",
            "ending_before": "2026-12-01T00:00:00Z",
        }
    ],
}
PAID = {
    "id": "in_fee",
    "customer": "cus",
    "status": "paid",
    "paid": True,
    "currency": "usd",
    "amount_paid": 25000,
    "metadata": {"metronome_id": "mi"},
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(mod.settings, "METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID", "voice-sub")
    monkeypatch.setattr(mod.settings, "STRIPE_SECRET_KEY", "test")
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.billing_repo, "record_voice_payment", record)
    monkeypatch.setattr(mod.billing_repo, "list_by_stripe_customer_id", AsyncMock(return_value=[{"owner_id": "org"}]))
    monkeypatch.setattr(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=ACCOUNT))
    monkeypatch.setattr(mod.stripe.Invoice, "retrieve", MagicMock(return_value=PAID))
    monkeypatch.setattr(mod, "refresh", AsyncMock())
    return record


async def test_paid_invoice_grants_only_its_paid_subscription_period(env):
    assert await mod._apply(ACCOUNT, INVOICE, PAID)
    assert env.await_args.kwargs["paid_through"] == "2026-12-01T00:00:00Z"
    assert env.await_args.kwargs["contract_id"] == "voice" and env.await_args.kwargs["state"] == "paid"


@pytest.mark.parametrize(
    "wrong",
    [
        {"contract_id": "credits"},
        {"customer_id": "other-mc"},
        {"external_invoice": {"billing_provider_type": "stripe", "invoice_id": "in_other"}},
    ],
)
async def test_other_contract_customer_or_invoice_cannot_grant_or_revoke(env, wrong):
    assert not await mod._apply(ACCOUNT, {**INVOICE, **wrong}, PAID)
    env.assert_not_awaited()


async def test_stale_failure_uses_canonical_paid_stripe_state_and_strong_customer_row(env):
    client = MagicMock(get_customer_invoice=AsyncMock(return_value=INVOICE))
    assert await mod.on_invoice({"type": "invoice.payment_failed", "data": {"object": PAID}}, client=client)
    assert env.await_args.kwargs["state"] == "paid"


async def test_failed_overage_invoice_blocks_without_fabricating_a_paid_period(env):
    failed = {
        **PAID,
        "status": "open",
        "paid": False,
        "attempted": True,
        "hosted_invoice_url": "https://invoice.stripe.com/owned",
    }
    assert await mod._apply(ACCOUNT, {**INVOICE, "line_items": []}, failed)
    assert env.await_args.kwargs["state"] == "failed" and env.await_args.kwargs["paid_through"] is None


async def test_export_lag_retries_instead_of_acknowledging_and_losing_payment(env):
    with pytest.raises(RuntimeError, match="retry"):
        await mod._apply(ACCOUNT, {**INVOICE, "external_invoice": None}, PAID)
    env.assert_not_awaited()


async def test_zero_stripe_payment_cannot_grant_a_nonzero_metronome_invoice(env):
    assert not await mod._apply(ACCOUNT, INVOICE, {**PAID, "amount_paid": 0})
    env.assert_not_awaited()


async def test_shared_stripe_customer_is_resolved_by_exact_metronome_customer_and_contract(env, monkeypatch):
    other = {
        **ACCOUNT,
        "owner_id": "other",
        "metronome_customer_id": "other-mc",
        "metronome_voice_contract_id": "other-voice",
    }
    monkeypatch.setattr(
        mod.billing_repo,
        "list_by_stripe_customer_id",
        AsyncMock(return_value=[{"owner_id": "other"}, {"owner_id": "org"}]),
    )
    monkeypatch.setattr(
        mod.billing_repo,
        "get_by_owner_id",
        AsyncMock(side_effect=lambda owner, **_: ACCOUNT if owner == "org" else other),
    )
    missing = httpx.HTTPStatusError(
        "not found", request=httpx.Request("GET", "https://api.test"), response=httpx.Response(404)
    )
    client = MagicMock(get_customer_invoice=AsyncMock(side_effect=[missing, INVOICE]))
    assert await mod.on_invoice({"data": {"object": PAID}}, client=client)
    assert env.await_args.args == ("org",)


async def test_readiness_requires_default_card_attached_to_exact_customer(env, monkeypatch):
    monkeypatch.setattr(
        mod.stripe.Customer,
        "retrieve",
        MagicMock(return_value={"id": "cus", "invoice_settings": {"default_payment_method": "pm"}}),
    )
    method = MagicMock(return_value={"customer": "other", "type": "card"})
    monkeypatch.setattr(mod.stripe.PaymentMethod, "retrieve", method)
    assert not await mod.default_card("cus")
    method.return_value = {"customer": "cus", "type": "card"}
    assert await mod.default_card("cus")
    method.return_value = {"customer": "cus", "type": "us_bank_account"}
    assert not await mod.default_card("cus")
