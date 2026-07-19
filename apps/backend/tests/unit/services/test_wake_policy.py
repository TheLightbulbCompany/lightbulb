"""wake_policy predicate matrix — the suppress decision must fail OPEN.

Every uncertain shape must read as "not parked" (legacy wake behavior), so
crashed boxes, killswitch stops, converting rows, paid owners, and billing
outages can never lose their poll-driven self-heal to the wake gate.
"""

from unittest.mock import AsyncMock

import pytest

from core.services import wake_policy


def _parked_row(**over):
    row = {"status": "stopped", "stop_reason": "trial_idle", "parked_at": "2026-07-19T01:00:00+00:00"}
    row.update(over)
    return row


def _trial_account():
    return {"subscription_status": "trial_local"}


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", True)
    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_DAILY_BUDGET", 6)
    wake_policy._suppressed_emitted.clear()


class TestIsParkedTrial:
    @pytest.mark.asyncio
    async def test_parked_trial_row_is_parked(self):
        assert await wake_policy.is_parked_trial("u1", _parked_row(), account=_trial_account()) is True

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
        from core.repositories import billing_repo

        monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=_trial_account()))
        assert await wake_policy.is_parked_trial("u1", _parked_row()) is True


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
