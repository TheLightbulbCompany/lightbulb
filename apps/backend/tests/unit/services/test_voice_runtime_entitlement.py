"""Collected voice coverage grants runtime use independently of the legacy Workspace payment fields."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from core.services import provision_gate as gate, trial_cap


def _voice(**extra):
    return {
        "owner_id": "org_voice",
        "subscription_status": "trial_local",
        "trial_end": 1,
        "metronome_voice_contract_id": "voice-contract",
        "voice_plan_status": "pending_payment",
        "voice_plan_paid_periods": {"invoice": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}},
        **extra,
    }


@pytest.mark.parametrize("legacy_status", [None, "trial_local", "canceled", "past_due"])
def test_current_paid_voice_entitles_runtime_without_rewriting_legacy_subscription(legacy_status):
    account = _voice(subscription_status=legacy_status)
    original = deepcopy(account)
    assert gate.is_subscription_active(account)
    assert gate.is_paid_subscription(account)
    assert gate.has_always_on_entitlement(account)
    assert gate.is_trial_blocked(account)  # no second Workspace checkout while voice already pays for runtime
    assert not trial_cap.is_trial_expired(account)
    assert account == original


@pytest.mark.parametrize(
    "changes",
    [
        {"voice_plan_paid_periods": {}},
        {"voice_plan_paid_periods": {"invoice": {"start": "2098-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}}},
        {"voice_plan_paid_periods": {"invoice": {"start": "2000-01-01T00:00:00Z", "end": "2001-01-01T00:00:00Z"}}},
        {"voice_plan_unpaid_invoice_ids": {"unpaid"}},
        {"voice_plan_ending_at": "2001-01-01T00:00:00Z"},
    ],
)
def test_missing_future_expired_unpaid_or_confirmed_ended_coverage_does_not_unlock_runtime(changes):
    account = _voice(subscription_status="canceled", **changes)
    assert not gate.is_subscription_active(account)
    assert not gate.is_paid_subscription(account)
    assert not gate.has_always_on_entitlement(account)


async def test_paid_voice_default_provider_passes_expired_trial_but_keeps_paid_daily_limit():
    account = _voice()
    with patch.object(gate.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)):
        assert await gate.evaluate_provision_gate(owner_id="org_voice") is None
    assert trial_cap.is_managed_capped(account)
    assert await trial_cap.reason_for_account("org_voice", account) is None
    capped = {**account, "openrouter_capped_day": trial_cap.utc_today()}
    with patch.object(gate.billing_repo, "get_by_owner_id", AsyncMock(return_value=capped)):
        blocked = await gate.evaluate_provision_gate(owner_id="org_voice")
    assert blocked.code == "included_daily_cap"
    assert await trial_cap.reason_for_account("org_voice", capped) == trial_cap.INCLUDED_CAP


async def test_explicit_managed_voice_owner_keeps_the_existing_credit_gate():
    account = _voice(provider_choice="openrouter_managed")
    with (
        patch.object(gate.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch("core.services.metronome_gate.is_owner_blocked", AsyncMock(return_value=True)) as blocked,
    ):
        result = await gate.evaluate_provision_gate(owner_id="org_voice")
    assert result.code == "credits_required"
    blocked.assert_awaited_once_with("org_voice")
    assert not trial_cap.is_managed_capped(account)


async def test_explicit_byo_choice_keeps_its_provider_requirement_for_paid_voice():
    account = _voice(provider_choice="byo_key")
    with patch.object(gate.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)):
        result = await gate.evaluate_provision_gate(owner_id="org_voice")
    assert result.code == "byo_provider_required"


async def test_failed_voice_coverage_does_not_rescue_an_expired_trial():
    account = _voice(voice_plan_unpaid_invoice_ids={"unpaid"})
    with patch.object(gate.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)):
        result = await gate.evaluate_provision_gate(owner_id="org_voice")
    assert result.code == "trial_expired"
    assert trial_cap.is_trial_expired(account)


def test_pending_voice_invoice_does_not_allow_a_second_workspace_subscription():
    account = _voice(voice_plan_paid_periods={})
    assert gate.is_trial_blocked(account)
    assert not gate.is_paid_subscription(account)


async def test_paid_voice_gets_no_free_trial_expiry_or_deletion_notice():
    from core.services import trial_lifecycle

    with patch.object(trial_lifecycle, "_send_once", AsyncMock()) as send:
        await trial_lifecycle.notices("org_voice", _voice(), now=2_000_000_000)
    send.assert_not_awaited()


async def test_voice_plan_cannot_be_misrepresented_as_paused_by_extending_leftover_trial():
    from core.services import trial_lifecycle

    with (
        patch.object(trial_lifecycle.billing_repo, "get_by_owner_id", AsyncMock(return_value=_voice())),
        patch.object(trial_lifecycle.billing_repo, "extend_trial", AsyncMock()) as extend,
        patch.object(trial_lifecycle.billing_repo, "claim_once", AsyncMock()) as claim,
    ):
        result = await trial_lifecycle.pause_once("org_voice")
    assert result["status"] == "voice_plan_managed"
    extend.assert_not_awaited()
    claim.assert_not_awaited()


async def test_paid_voice_row_never_reports_as_a_parked_free_trial(monkeypatch):
    from core.services import wake_policy

    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", True)
    assert not await wake_policy.is_parked_trial("org_voice", {"last_interactive_at": 1}, account=_voice())


async def test_trial_idle_sweep_rechecks_paid_voice_after_its_claim():
    from core.services import durable_activity
    from tests.unit.services.test_durable_activity import _patches

    metric, patches = _patches(has_open_runs=False, row={"owner_id": "org_voice", "last_interactive_at": 0})
    for item in patches:
        item.start()
    try:
        with patch.object(
            durable_activity.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                side_effect=[
                    {"subscription_status": "trial_local"},
                    _voice(),
                ]
            ),
        ):
            await durable_activity.sweep_trial_idle_once(now=10_000)
    finally:
        for item in patches:
            item.stop()
    metric.assert_not_called()


async def test_trial_idle_sweep_skips_already_collected_voice_plan_before_claiming():
    from core.services import durable_activity
    from tests.unit.services.test_durable_activity import _patches

    metric, patches = _patches(has_open_runs=False, row={"owner_id": "org_voice", "last_interactive_at": 0})
    for item in patches:
        item.start()
    try:
        with (
            patch.object(durable_activity.billing_repo, "get_by_owner_id", AsyncMock(return_value=_voice())),
            patch.object(durable_activity.container_repo, "try_claim_idle_stop", AsyncMock()) as claim,
        ):
            await durable_activity.sweep_trial_idle_once(now=10_000)
        claim.assert_not_awaited()
    finally:
        for item in patches:
            item.stop()
    metric.assert_not_called()
