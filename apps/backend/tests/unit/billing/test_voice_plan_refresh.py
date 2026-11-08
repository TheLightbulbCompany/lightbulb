from unittest.mock import AsyncMock

import pytest

from core.billing import voice_plan_payments
from core.services import openrouter_keys, owner_facts, proactive, provision_dispatch, container_provision


def _account():
    return {
        "owner_id": "org",
        "subscription_status": "trial_local",
        "trial_end": 1,
        "metronome_voice_contract_id": "voice",
        "voice_plan_paid_periods": {"fee": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}},
    }


@pytest.fixture
def env(monkeypatch):
    account = _account()
    monkeypatch.setattr(voice_plan_payments.settings, "OPENROUTER_WORKSPACE_ID", "workspace")
    monkeypatch.setattr(voice_plan_payments.billing_repo, "get_by_owner_id", AsyncMock(return_value=account))

    async def save(_owner, **fields):
        account["voice_plan_runtime_sync_revision"] = fields["revision"]

    marker = AsyncMock(side_effect=save)
    monkeypatch.setattr(voice_plan_payments.billing_repo, "set_voice_runtime_sync_revision", marker)
    facts, relimit, dispatch, ensure = AsyncMock(), AsyncMock(), AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(owner_facts, "write_owner_facts", facts)
    monkeypatch.setattr(openrouter_keys, "relimit_owner_key", relimit)
    monkeypatch.setattr(provision_dispatch, "dispatch_reconcile", dispatch)
    monkeypatch.setattr(proactive, "ensure", ensure)
    return account, marker, facts, relimit, dispatch, ensure


async def test_collected_voice_refreshes_existing_facts_key_limit_and_durable_runtime_once(env):
    _, marker, facts, relimit, dispatch, _ = env
    await voice_plan_payments.refresh("org")
    await voice_plan_payments.refresh("org")
    facts.assert_awaited_once_with("org")
    relimit.assert_awaited_once_with("org")
    dispatch.assert_awaited_once_with("org")
    marker.assert_awaited_once()


async def test_failed_runtime_dispatch_remains_repairable_after_invoice_already_collected(env):
    _, marker, _, _, dispatch, _ = env
    dispatch.return_value = False
    with pytest.raises(RuntimeError, match="retry"):
        await voice_plan_payments.refresh("org")
    marker.assert_not_awaited()
    dispatch.return_value = True
    await voice_plan_payments.refresh("org")
    marker.assert_awaited_once()


async def test_payment_failure_refreshes_facts_and_deactivates_paid_autonomy_without_new_provision(env):
    account, _, facts, relimit, dispatch, ensure = env
    account["voice_plan_unpaid_invoice_ids"] = {"debt"}
    await voice_plan_payments.refresh("org")
    facts.assert_awaited_once()
    relimit.assert_awaited_once()
    dispatch.assert_not_awaited()
    ensure.assert_awaited_once_with("org")


def test_fresh_voice_keeps_bounded_included_key_budget_and_reports_voice_facts(monkeypatch):
    account = _account()
    monkeypatch.setattr(openrouter_keys.settings, "OPENROUTER_TRIAL_KEY_DAILY_LIMIT_USD", 0.1)
    monkeypatch.setattr(openrouter_keys.settings, "OPENROUTER_BASE_KEY_DAILY_LIMIT_USD", 0.9)
    assert openrouter_keys.limit_for_account(account) == 0.9
    assert container_provision.provider_choice_from_row(
        {**account, "provider_choice": "byo_key", "byo_provider": "openai"}
    ) == ("byo_key", "openai")
    facts = owner_facts.build_owner_facts("org", account)
    assert "$250/mo" in facts["plan"] and "Free trial" not in facts["plan"] and "$50" not in facts["plan"]
    assert facts["voice_plan"]["collected_coverage_active"] is True
