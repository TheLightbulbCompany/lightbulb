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


# ---------------------------------------------------------------------------
# Wake fast path — direct scale-up for cleanly stopped rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_clean_row_takes_the_direct_scaleup_fast_path(monkeypatch):
    """A stopped row with no needs_provision marker wakes via
    start_user_service (one update_service call), NOT the SFN executor —
    the provisioner VM cold-launch is ~90-120s of overhead for a wake."""
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock()
    dispatched = AsyncMock()
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", return_value=None),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "stopped"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.dispatch_provision", dispatched),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "started"
    manager.start_user_service.assert_awaited_once_with("u1")
    dispatched.assert_not_called()


@pytest.mark.asyncio
async def test_needs_provision_marker_routes_the_wake_through_the_full_executor():
    """A task-def-shaped config change (provider switch) marked the row —
    the next wake must run the FULL provision once to apply it."""
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock()
    dispatched = AsyncMock()
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", return_value=None),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "stopped", "needs_provision": True}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.dispatch_provision", dispatched),
        patch("core.services.container_wake.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "dispatched"
    manager.start_user_service.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_failure_falls_back_to_the_full_executor():
    """The wake must never be lost to the optimization: any fast-path
    failure (gate re-check refusal, ECS hiccup) falls back to dispatch."""
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock(side_effect=RuntimeError("ecs blip"))
    dispatched = AsyncMock()
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", return_value=None),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "stopped"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.dispatch_provision", dispatched),
        patch("core.services.container_wake.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "dispatched"
    manager.start_user_service.assert_awaited_once()


@pytest.mark.asyncio
async def test_errored_row_always_takes_the_full_executor():
    """A failed provision needs the runner (config rewrite, task-def
    re-register, fenced retries) — never a bare scale-up."""
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock()
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", return_value=None),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "error"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "dispatched"
    manager.start_user_service.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_cap_refusal_blocks_instead_of_dispatching():
    """A cap refusal raised by the chokepoint's own re-check is a POLICY
    outcome — dispatching would bypass it (the SFN runner starts with
    force=True). The wake must surface the structured gate card."""
    from core.containers.ecs_manager import START_REFUSED_CAP, ContainerStartRefused
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock(side_effect=ContainerStartRefused(START_REFUSED_CAP, "u1", "capped"))
    dispatched = AsyncMock()
    gate = MagicMock()
    gate.to_payload.return_value = {"code": "trial_daily_cap"}
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", side_effect=[None, gate]),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "stopped"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.dispatch_provision", dispatched),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "blocked"
    assert result.gate_payload == {"code": "trial_daily_cap"}
    dispatched.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_reprovision_refusal_falls_through_to_dispatch():
    """A needs_provision refusal raced our row read — the full provision IS
    the remedy, so this one refusal DOES route to dispatch."""
    from core.containers.ecs_manager import START_REFUSED_REPROVISIONING, ContainerStartRefused
    from core.services import container_wake

    manager = MagicMock()
    manager.start_user_service = AsyncMock(
        side_effect=ContainerStartRefused(START_REFUSED_REPROVISIONING, "u1", "config pending")
    )
    dispatched = AsyncMock()
    with (
        patch("core.services.provision_gate.gate_for_trial_reason", return_value=None),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
        patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "u1", "status": "stopped"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=manager),
        patch("core.services.container_wake.dispatch_provision", dispatched),
        patch("core.services.container_wake.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        result = await container_wake.request_wake("u1", reason="interaction", is_admin=True)

    assert result.outcome == "dispatched"
