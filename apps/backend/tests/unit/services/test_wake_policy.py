"""wake_policy predicate matrix — the suppress decision must fail OPEN.

Every uncertain shape must read as "not parked" (legacy wake behavior), so
crashed boxes, killswitch stops, converting rows, paid owners, and billing
outages can never lose their poll-driven self-heal to the wake gate.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from core.services import wake_policy


def _parked_row(**over):
    row = {"status": "stopped", "stop_reason": "trial_idle", "parked_at": "2026-07-19T01:00:00+00:00"}
    row.update(over)
    return row


def _trial_account():
    return {"subscription_status": "trial_local"}


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", True)
    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_DAILY_BUDGET", 6)
    wake_policy._suppressed_emitted.clear()


@pytest.fixture
def trial_account():
    return {"subscription_status": "trial_local"}


@pytest.fixture
def paid_account():
    return {"subscription_status": "active"}


class TestIsParkedTrial:
    # NOTE: `test_parked_trial_row_is_parked` was deleted. `_parked_row()`'s
    # status="stopped"/stop_reason="trial_idle" shape only ever mattered via the ECS leg of
    # `is_parked_trial`, unreachable now that `host_for` is pinned to "agentcore" fleet-wide
    # (the row has no `last_interactive_at`, so the surviving AgentCore leg fails OPEN --
    # False, not True). The happy-path "parked" result now lives entirely in
    # TestIsParkedTrialAgentCore::test_an_idle_agentcore_trial_row_reads_as_parked below.

    @pytest.mark.asyncio
    async def test_flag_off_is_never_parked(self, monkeypatch):
        monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", False)
        assert await wake_policy.is_parked_trial("u1", _parked_row(), account=_trial_account()) is False

    @pytest.mark.asyncio
    async def test_missing_row_not_parked(self):
        assert await wake_policy.is_parked_trial("u1", None, account=_trial_account()) is False

    @pytest.mark.asyncio
    async def test_running_row_not_parked(self):
        row = _parked_row(status="running")
        assert await wake_policy.is_parked_trial("u1", row, account=_trial_account()) is False

    @pytest.mark.asyncio
    async def test_error_row_not_parked(self):
        """Crashed boxes keep their poll-driven self-heal."""
        row = _parked_row(status="error")
        assert await wake_policy.is_parked_trial("u1", row, account=_trial_account()) is False

    @pytest.mark.asyncio
    async def test_foreign_stop_reason_not_parked(self):
        """killswitch/admin/reprovision stops must never read as parked."""
        for reason in ("killswitch", "admin", "reprovision", "manual", None):
            row = _parked_row()
            if reason is None:
                row.pop("stop_reason")
            else:
                row["stop_reason"] = reason
            assert await wake_policy.is_parked_trial("u1", row, account=_trial_account()) is False, reason

    @pytest.mark.asyncio
    async def test_conversion_pending_not_parked(self):
        """The conversion reprovision owns the row — never suppress its poll."""
        row = _parked_row(conversion_pending=True)
        assert await wake_policy.is_parked_trial("u1", row, account=_trial_account()) is False

    @pytest.mark.asyncio
    async def test_paid_owner_not_parked(self):
        assert (
            await wake_policy.is_parked_trial("u1", _parked_row(), account={"subscription_status": "active"}) is False
        )

    @pytest.mark.asyncio
    async def test_no_billing_row_not_parked(self):
        assert await wake_policy.is_parked_trial("u1", _parked_row(), account={}) is False

    @pytest.mark.asyncio
    async def test_billing_read_failure_fails_open(self, monkeypatch):
        from core.repositories import billing_repo

        monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb down")))
        assert await wake_policy.is_parked_trial("u1", _parked_row()) is False

    @pytest.mark.asyncio
    async def test_billing_row_fetched_when_not_supplied(self, monkeypatch):
        """`_is_trial`'s "fetch from the repo when `account` isn't supplied" behavior is
        shared by both legs of `is_parked_trial` -- exercised here via the surviving
        AgentCore leg (`host_for` is pinned to "agentcore" fleet-wide), with a row shaped
        so the fetched trial account is what tips it into "parked"."""
        from core.repositories import billing_repo

        monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=_trial_account()))
        row = {"status": "running", "last_interactive_at": _iso(60 * 9)}
        assert await wake_policy.is_parked_trial("u1", row) is True


class TestLegacyWakeBudget:
    @pytest.mark.asyncio
    async def test_under_budget_allows(self, monkeypatch):
        from core.repositories import container_repo

        monkeypatch.setattr(container_repo, "record_wake_debit", AsyncMock(return_value=6))
        assert await wake_policy.legacy_wake_allowed("u1") is True

    @pytest.mark.asyncio
    async def test_over_budget_suppresses(self, monkeypatch):
        from core.repositories import container_repo

        monkeypatch.setattr(container_repo, "record_wake_debit", AsyncMock(return_value=7))
        assert await wake_policy.legacy_wake_allowed("u1") is False

    @pytest.mark.asyncio
    async def test_flag_off_allows_without_debit(self, monkeypatch):
        from core.repositories import container_repo

        debit = AsyncMock(return_value=999)
        monkeypatch.setattr(container_repo, "record_wake_debit", debit)
        monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", False)
        assert await wake_policy.legacy_wake_allowed("u1") is True
        debit.assert_not_called()

    @pytest.mark.asyncio
    async def test_debit_failure_fails_open(self, monkeypatch):
        from core.repositories import container_repo

        monkeypatch.setattr(container_repo, "record_wake_debit", AsyncMock(side_effect=RuntimeError("ddb")))
        assert await wake_policy.legacy_wake_allowed("u1") is True


class TestSuppressedEmissionDedup:
    def test_emits_once_per_park_episode(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(wake_policy, "put_metric", lambda name, dimensions=None: calls.append(dimensions))
        row = _parked_row()
        wake_policy.note_wake_suppressed("u1", row, door="status_poll", reason="gated")
        wake_policy.note_wake_suppressed("u1", row, door="status_poll", reason="gated")
        assert len(calls) == 1
        # A NEW park episode (fresh parked_at) re-emits.
        row2 = _parked_row(parked_at="2026-07-19T09:00:00+00:00")
        wake_policy.note_wake_suppressed("u1", row2, door="status_poll", reason="gated")
        assert len(calls) == 2
        # Different door tracks independently.
        wake_policy.note_wake_suppressed("u1", row2, door="status_legacy", reason="budget")
        assert len(calls) == 3


class TestIsParkedTrialAgentCore:
    """AgentCore has no stopped/stop_reason to read (stop_user_service no-ops for this host —
    report 04 risk 2). The park signal is elapsed time since last_interactive_at instead."""

    @pytest.mark.asyncio
    async def test_an_idle_agentcore_trial_row_reads_as_parked(self, trial_account):
        row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(60 * 5)}
        assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is True

    @pytest.mark.asyncio
    async def test_a_recently_used_agentcore_trial_row_is_not_parked(self, trial_account):
        row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(5)}
        assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False

    @pytest.mark.asyncio
    async def test_an_agentcore_row_with_no_last_interactive_at_is_not_parked(self, trial_account):
        row = {"runtime_host": "agentcore", "status": "running"}
        # Fails OPEN, like every other uncertainty in this predicate: a fresh row must not show
        # "Agents asleep" before the owner has ever sent anything.
        assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False

    @pytest.mark.asyncio
    async def test_a_paid_agentcore_row_is_never_parked(self, paid_account):
        row = {"runtime_host": "agentcore", "status": "running", "last_interactive_at": _iso(60 * 9)}
        assert await wake_policy.is_parked_trial("org_a", row, account=paid_account) is False

    # NOTE: `test_the_ecs_predicate_is_untouched` was deleted -- `host_for` no longer reads
    # `runtime_host` at all (it's pinned to "agentcore" fleet-wide, deletion PR-1), so a
    # `runtime_host: "ecs"` row now takes the exact same AgentCore leg as every other row:
    # the stopped/trial_idle shape reads as NOT parked (fails open, no `last_interactive_at`),
    # and the "running" shape reads as parked once idle long enough -- the opposite of what
    # this test asserted.
