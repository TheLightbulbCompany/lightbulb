"""wake_policy predicate matrix — the parked report must fail OPEN.

Every uncertain shape must read as "not parked", so a fresh workspace, a paid
owner and a billing outage never show "Agents asleep".

AWS parks an idle microVM invisibly, so the row stays ``status="running"``
forever and the park signal is elapsed time since ``last_interactive_at``
against the runtime's own idle timeout.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from core.services import wake_policy


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _row(**over):
    row = {"status": "running", "last_interactive_at": _iso(60 * 9), "parked_at": "2026-07-19T01:00:00+00:00"}
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", True)


@pytest.fixture
def trial_account():
    return {"subscription_status": "trial_local"}


@pytest.fixture
def paid_account():
    return {"subscription_status": "active"}


class TestIsParkedTrial:
    @pytest.mark.asyncio
    async def test_an_idle_trial_row_reads_as_parked(self, trial_account):
        assert await wake_policy.is_parked_trial("org_a", _row(), account=trial_account) is True

    @pytest.mark.asyncio
    async def test_a_recently_used_trial_row_is_not_parked(self, trial_account):
        assert (
            await wake_policy.is_parked_trial("org_a", _row(last_interactive_at=_iso(1)), account=trial_account)
            is False
        )

    @pytest.mark.asyncio
    async def test_flag_off_is_never_parked(self, monkeypatch, trial_account):
        monkeypatch.setattr(wake_policy.settings, "TRIAL_WAKE_GATE_ENABLED", False)
        assert await wake_policy.is_parked_trial("org_a", _row(), account=trial_account) is False

    @pytest.mark.asyncio
    async def test_missing_row_not_parked(self, trial_account):
        assert await wake_policy.is_parked_trial("org_a", None, account=trial_account) is False

    @pytest.mark.asyncio
    async def test_no_last_interactive_at_is_not_parked(self, trial_account):
        """Fails OPEN like every other uncertainty: a fresh workspace must not show
        "Agents asleep" before the owner has ever sent anything."""
        row = _row()
        row.pop("last_interactive_at")
        assert await wake_policy.is_parked_trial("org_a", row, account=trial_account) is False

    @pytest.mark.asyncio
    async def test_unparseable_last_interactive_at_is_not_parked(self, trial_account):
        assert (
            await wake_policy.is_parked_trial("org_a", _row(last_interactive_at="not-a-date"), account=trial_account)
            is False
        )

    @pytest.mark.asyncio
    async def test_paid_owner_not_parked(self, paid_account):
        assert await wake_policy.is_parked_trial("org_a", _row(), account=paid_account) is False

    @pytest.mark.asyncio
    async def test_no_billing_row_not_parked(self):
        assert await wake_policy.is_parked_trial("org_a", _row(), account={}) is False

    @pytest.mark.asyncio
    async def test_billing_read_failure_fails_open(self, monkeypatch):
        from core.repositories import billing_repo

        monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb down")))
        assert await wake_policy.is_parked_trial("org_a", _row()) is False

    @pytest.mark.asyncio
    async def test_billing_row_fetched_when_not_supplied(self, monkeypatch, trial_account):
        from core.repositories import billing_repo

        monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=trial_account))
        assert await wake_policy.is_parked_trial("org_a", _row()) is True
