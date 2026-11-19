"""T19: the one pause."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import trial_lifecycle

NOW = 1_800_000_000
DAY = 86400


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
