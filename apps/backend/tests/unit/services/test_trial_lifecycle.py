"""T19: day-27 mail, the deletion notice and the one pause."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import agent_mail_owner_send, trial_lifecycle

NOW = 1_800_000_000
DAY = 86400


@pytest.fixture
def mail(monkeypatch):
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(agent_mail_owner_send, "send_to_owner", send)
    monkeypatch.setattr(trial_lifecycle.trial_recap, "room_activity", AsyncMock(return_value=(3, 2)))
    claims = {"won": True}
    monkeypatch.setattr(trial_lifecycle.billing_repo, "claim_once", AsyncMock(side_effect=lambda *a: claims["won"]))
    monkeypatch.setattr(trial_lifecycle.billing_repo, "release_once", AsyncMock())
    return send, claims


@pytest.mark.asyncio
async def test_day_27_sends_the_recap_once_from_the_ceo(mail):
    send, claims = mail
    await trial_lifecycle.notices("org_1", {"trial_end": NOW + 3 * DAY + 3600}, NOW)
    kwargs = send.await_args.kwargs
    assert kwargs["purpose"] == "trial_recap" and "agent_id" not in kwargs  # the CEO is the default sender
    assert "2 agents did work for 3 people" in kwargs["body_text"]
    claims["won"] = False
    send.reset_mock()
    await trial_lifecycle.notices("org_1", {"trial_end": NOW + 3 * DAY + 3600}, NOW)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_send_releases_the_claim_for_a_retry(mail):
    send, _ = mail
    send.return_value = "error"
    await trial_lifecycle.notices("org_1", {"trial_end": NOW + 3 * DAY + 3600}, NOW)
    trial_lifecycle.billing_repo.release_once.assert_awaited_once_with("org_1", "trial_recap_emailed_at")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end", "purpose"), [(NOW - 91 * DAY, "deletion_notice"), (NOW - 89 * DAY, None), (NOW + 10 * DAY, None)]
)
async def test_deletion_notice_only_after_90_reading_only_days(mail, end, purpose):
    send, _ = mail
    await trial_lifecycle.notices("org_1", {"trial_end": end}, NOW)
    assert (send.await_args.kwargs["purpose"] if send.await_args else None) == purpose


@pytest.mark.asyncio
async def test_one_pause_moves_the_trial_30_days_and_never_twice(monkeypatch):
    row = {"owner_id": "org_1", "subscription_status": "trial_local", "trial_end": NOW - DAY}
    monkeypatch.setattr(trial_lifecycle.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claims = iter([True, False])
    monkeypatch.setattr(trial_lifecycle.billing_repo, "claim_once", AsyncMock(side_effect=lambda *a: next(claims)))
    extend = AsyncMock()
    monkeypatch.setattr(trial_lifecycle.billing_repo, "extend_trial", extend)
    from core.services import proactive

    arm = AsyncMock()
    monkeypatch.setattr(proactive, "ensure", arm)

    assert await trial_lifecycle.pause_once("org_1", NOW) == {"status": "paused", "until": NOW + 30 * DAY}
    extend.assert_awaited_once_with("org_1", trial_end=NOW + 30 * DAY)
    arm.assert_awaited_once_with("org_1")  # the expired trial's paused automation comes back
    assert await trial_lifecycle.pause_once("org_1", NOW) == {"status": "pause_used"}


@pytest.mark.asyncio
async def test_pause_on_a_live_subscription_pauses_collection(monkeypatch):
    import stripe

    row = {"owner_id": "org_1", "stripe_subscription_status": "active", "stripe_subscription_id": "sub_1"}
    monkeypatch.setattr(trial_lifecycle.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(trial_lifecycle.billing_repo, "claim_once", AsyncMock(return_value=True))
    modify = MagicMock()
    monkeypatch.setattr(stripe.Subscription, "modify", modify)
    await trial_lifecycle.pause_once("org_1", NOW)
    assert modify.call_args.kwargs["pause_collection"] == {"behavior": "void", "resumes_at": NOW + 30 * DAY}


@pytest.mark.asyncio
async def test_a_failed_rearm_falls_back_to_a_durable_reconcile(monkeypatch):
    from core.services import proactive, provision_dispatch

    monkeypatch.setattr(proactive, "ensure", AsyncMock(side_effect=RuntimeError("down")))
    reconcile = AsyncMock(return_value=True)
    monkeypatch.setattr(provision_dispatch, "dispatch_reconcile", reconcile)
    await trial_lifecycle._rearm_automation("org_1")
    reconcile.assert_awaited_once_with("org_1")
