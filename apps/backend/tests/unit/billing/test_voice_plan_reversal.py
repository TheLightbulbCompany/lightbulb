"""A refunded or disputed voice-plan payment ends the plan and files one Needs-you card."""

from unittest.mock import AsyncMock, patch

import pytest

from core.billing import voice_plan
from core.config import settings
from core.services import stripe_webhook_handlers as h

pytestmark = pytest.mark.asyncio

SUB = {
    "id": "sub_1",
    "status": "active",
    "metadata": {"purpose": "voice_plan", "workspace_id": "org_1"},
    "items": {"data": [{"price": {"id": "price_voice"}}]},
}


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_VOICE_PLAN_PRICE_ID", "price_voice")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk")


def _run(sub_status="active", other_sub=False, clover=False):
    calls = []
    kwargs = {}

    async def run(fn, *a, **k):
        name = fn.__qualname__
        calls.append(name)
        if name.endswith("Charge.retrieve"):
            return {"id": "ch_1", "payment_intent": "pi_1"} if clover else {"id": "ch_1", "invoice": "in_1"}
        if name.endswith("InvoicePayment.list"):
            kwargs.update(k)
            return {"data": [{"invoice": "in_1"}]}
        if name.endswith("Invoice.retrieve"):
            return {"parent": {"subscription_details": {"subscription": "sub_1"}}}
        if name.endswith("Subscription.retrieve"):
            sub = {**SUB, "status": sub_status}
            return {**sub, "items": {"data": [{"price": {"id": "other"}}]}} if other_sub else sub
        return {}

    run.kwargs = kwargs
    return run, calls


async def _fire(event, run):
    announce = AsyncMock()
    with (
        patch("core.dynamodb.run_in_thread", run),
        patch.object(voice_plan, "run_in_thread", run),
        patch.object(voice_plan, "sync_subscription", AsyncMock()) as synced,
        patch.object(voice_plan, "announce_reversal", announce),
    ):
        await h.handle_voice_plan_reversal(event, None)
    return synced, announce


def _refund(refunded):
    return {"type": "charge.refunded", "data": {"object": {"id": "ch_1", "refunded": refunded}}}


DISPUTE = {"type": "charge.dispute.created", "data": {"object": {"charge": "ch_1"}}}


async def test_full_refund_cancels_syncs_and_files_card():
    run, calls = _run()
    synced, announce = await _fire(_refund(True), run)
    assert calls[-1].endswith("Subscription.cancel")
    synced.assert_awaited_once()
    announce.assert_awaited_once_with("org_1")


async def test_dispute_ends_plan_and_replay_does_not_cancel_twice():
    run, calls = _run(sub_status="canceled")  # a redelivery finds it already canceled
    synced, announce = await _fire(DISPUTE, run)
    assert not any(c.endswith("Subscription.cancel") for c in calls)
    synced.assert_awaited_once()
    announce.assert_awaited_once()


async def test_partial_refund_leaves_entitlement_and_no_card():
    run, calls = _run()
    synced, announce = await _fire(_refund(False), run)
    assert calls == []
    synced.assert_not_awaited()
    announce.assert_not_awaited()


async def test_charge_for_another_plan_is_ignored():
    run, calls = _run(other_sub=True)
    synced, announce = await _fire(_refund(True), run)
    assert not any(c.endswith("Subscription.cancel") for c in calls)
    announce.assert_not_awaited()


async def test_routed_for_both_events():
    assert h.STRIPE_WEBHOOK_HANDLERS["charge.refunded"] is h.handle_voice_plan_reversal
    assert h.STRIPE_WEBHOOK_HANDLERS["charge.dispute.created"] is h.handle_voice_plan_reversal


async def test_clover_charge_without_invoice_resolves_through_invoice_payment():
    run, calls = _run(clover=True)
    synced, announce = await _fire(_refund(True), run)
    assert run.kwargs["payment"] == {"type": "payment_intent", "payment_intent": "pi_1"}
    assert calls[-1].endswith("Subscription.cancel")
    announce.assert_awaited_once_with("org_1")


async def test_subscription_without_voice_purpose_is_not_cancelled():
    run, calls = _run()
    base = run

    async def run2(fn, *a, **k):
        out = await base(fn, *a, **k)
        if fn.__qualname__.endswith("Subscription.retrieve"):
            return {**out, "metadata": {"workspace_id": "org_1"}}
        return out

    synced, announce = await _fire(_refund(True), run2)
    assert not any(c.endswith("Subscription.cancel") for c in calls)
    announce.assert_not_awaited()


async def test_card_renders_with_support_copy():
    from core.services import activity

    row = {"kind": "reconnect", "provider": "voice_plan", "createdAt": "2026-11-10T10:00:00+00:00"}
    (item,) = await activity._render_reconnect_rows("org_1", [row], None)
    assert item.kind == "reconnect" and item.state == "open"
    assert item.summary.title == "Your voice plan has ended"
    assert "hello@thelightbulb.company" in item.summary.reason
