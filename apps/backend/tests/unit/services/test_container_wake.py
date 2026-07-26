"""container_wake branch matrix — both verbs, every outcome.

The silent (chat-send) path must be at least as strict as the explicit verb:
trial gates AND the provision gate stand it down, and operator-stopped rows
(admin/killswitch) are never silently restarted by a message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import container_wake


def _row(**over):
    row = {"status": "stopped", "stop_reason": "trial_idle"}
    row.update(over)
    return row


def _ctx(row, *, trial_reason=None, gate=None):
    gate_obj = None
    if gate is not None:
        gate_obj = MagicMock()
        gate_obj.to_payload.return_value = gate
    return [
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=trial_reason)),
        patch(
            "core.services.provision_gate.evaluate_provision_gate",
            new=AsyncMock(return_value=gate_obj),
        ),
        patch("core.repositories.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
        patch("core.services.container_wake.dispatch_provision", new=AsyncMock()),
    ]


async def _silent(row, **kw):
    ps = _ctx(row, **kw)
    with ps[0], ps[1], ps[2], ps[3] as dispatch:
        outcome = await container_wake.request_wake_silent("u1")
        return outcome, dispatch


async def _explicit(row, **kw):
    ps = _ctx(row, **kw)
    with ps[0], ps[1], ps[2], ps[3] as dispatch:
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)
        return result, dispatch


class TestRequestWakeSilent:
    @pytest.mark.asyncio
    async def test_parked_row_dispatches(self):
        outcome, dispatch = await _silent(_row())
        assert outcome == "dispatched"
        dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_trial_gate_blocks(self):
        outcome, dispatch = await _silent(_row(), trial_reason="trial_daily_cap")
        assert outcome == "blocked"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_provision_gate_blocks(self):
        """A canceled/unpaid owner's chat message must never resurrect a box."""
        outcome, dispatch = await _silent(_row(), gate={"blocked": {"code": "subscription_required"}})
        assert outcome == "blocked"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_stops_never_silently_restarted(self):
        for reason in ("admin", "killswitch"):
            outcome, dispatch = await _silent(_row(stop_reason=reason))
            assert outcome == "operator_stopped", reason
            dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversion_pending_stands_down(self):
        outcome, dispatch = await _silent(_row(conversion_pending=True))
        assert outcome == "converting"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_row_noop(self):
        outcome, dispatch = await _silent(_row(status="running"))
        assert outcome == "already_running"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_row_noop(self):
        outcome, dispatch = await _silent(None)
        assert outcome == "no_row"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_raises(self):
        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ):
            assert await container_wake.request_wake_silent("u1") == "error"


class TestRequestWake:
    @pytest.mark.asyncio
    async def test_stopped_row_dispatches(self):
        result, dispatch = await _explicit(_row())
        assert result.outcome == "dispatched"
        dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_row_dispatches(self):
        result, dispatch = await _explicit(_row(status="error", stop_reason=None))
        assert result.outcome == "dispatched"
        dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_trial_gate_returns_payload(self):
        result, dispatch = await _explicit(_row(), trial_reason="trial_expired")
        assert result.outcome == "blocked"
        assert result.gate_payload is not None
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_provision_gate_returns_payload(self):
        result, dispatch = await _explicit(_row(), gate={"blocked": {"code": "subscription_required"}})
        assert result.outcome == "blocked"
        assert result.gate_payload == {"blocked": {"code": "subscription_required"}}
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_flight_provisioning_noop(self):
        with patch("core.repositories.container_repo.is_provision_claim_stale", return_value=False):
            result, dispatch = await _explicit(_row(status="provisioning"))
        assert result.outcome == "in_flight"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_row_defers_to_status_route(self):
        result, dispatch = await _explicit(None)
        assert result.outcome == "no_row"
        dispatch.assert_not_called()
