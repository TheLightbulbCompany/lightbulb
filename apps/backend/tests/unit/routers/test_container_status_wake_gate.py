"""GET /container/status wake-gating matrix.

The suppress decision applies ONLY to a parked trial row and ONLY shapes the
dispatch; gates still 402 first, error rows keep their unconditional
self-heal, and legacy (un-flagged) clients keep wake-on-read under the daily
budget.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers.container import container_status


def _auth(user_id: str = "user_w") -> AuthContext:
    return AuthContext(user_id=user_id, org_id="org_w", org_role="org:admin", email="w@example.com")


def _parked_row() -> dict:
    return {
        "service_name": "openclaw-user_w-abc",
        "status": "stopped",
        "stop_reason": "trial_idle",
        "parked_at": "2026-07-19T01:00:00+00:00",
    }


def _mock_manager(row: dict) -> MagicMock:
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(None, None))
    manager.get_service_status = AsyncMock(return_value=row)
    manager.has_live_service = AsyncMock(return_value=False)
    return manager


async def _compute_status(_owner_id: str, *, container: dict, onboarding: dict) -> dict:
    parked = container.get("stop_reason") == "trial_idle"
    return {
        "lifecycle": "parked" if parked else ("error" if container.get("status") == "error" else "provisioning"),
        "runtime_phase": "provisioning",
        "chat_available": False,
        "container_status": container.get("status"),
        "container_substatus": container.get("substatus"),
        "parked": parked,
        "provider": {"subscription_status": "active"},
        "teams_state": "ready",
    }


def _patches(row: dict, *, parked: bool, budget_ok: bool = True, trial_reason=None):
    """The full patch stack for driving the auto-retry branch in isolation."""
    return [
        patch("routers.container.get_ecs_manager", return_value=_mock_manager(row)),
        patch("routers.container.maybe_restore_trial_bedrock", new=AsyncMock()),
        patch("routers.container.maybe_resume_stuck_conversion", new=AsyncMock(return_value=False)),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=trial_reason)),
        patch("routers.container._assert_provision_allowed", new=AsyncMock()),
        patch("routers.container.wake_policy.is_parked_trial", new=AsyncMock(return_value=parked)),
        patch("routers.container.wake_policy.legacy_wake_allowed", new=AsyncMock(return_value=budget_ok)),
        patch("routers.container.dispatch_provision", new=AsyncMock()),
        patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()),
        patch("routers.container.ensure_user_ready", new=AsyncMock()),
        patch("routers.container.readiness_state.resolve_onboarding_state", new=AsyncMock(return_value={})),
        patch("routers.container.readiness_state.compute_status", new=_compute_status),
    ]


async def _call(wake, patches):
    with ExitStack() as stack:
        entered = [stack.enter_context(item) for item in patches]
        body = await container_status(auth=_auth(), wake=wake)
        return body, entered[7], entered[6]


class TestWakeGate:
    @pytest.mark.asyncio
    async def test_parked_wake0_suppresses_and_reports_parked(self):
        body, dispatch, _ = await _call("0", _patches(_parked_row(), parked=True))
        # Explicit wake=0 override: lifecycle="parked" + parked=True +
        # wake_retry_after_s (spec PR-9, CLIENT-HOOK) -- NOT the old
        # phase="parked" flat string, which no longer exists on the wire.
        assert body["lifecycle"] == "parked"
        assert body["parked"] is True
        assert body["wake_retry_after_s"] == 30
        assert body["container_status"] == "stopped"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_parked_legacy_under_budget_wakes(self):
        body, dispatch, budget = await _call(None, _patches(_parked_row(), parked=True, budget_ok=True))
        dispatch.assert_called_once()
        budget.assert_awaited_once()
        assert body["container_status"] == "provisioning"
        assert body["container_substatus"] == "auto_retry"

    @pytest.mark.asyncio
    async def test_parked_legacy_over_budget_suppresses_reflecting_the_composed_contract(self):
        # No more "legacy bundle" field-set distinction (spec PR-9,
        # CLIENT-HOOK): _status_response emits the ONE composed contract
        # always. This branch's own is_parked_trial check (mocked True here)
        # already flows into the SAME contract compute_status composed, so
        # `parked`/`lifecycle` reflect that truth even without the explicit
        # wake=0 override -- only `wake_retry_after_s` is specific to that
        # override, and is absent here.
        body, dispatch, _ = await _call(None, _patches(_parked_row(), parked=True, budget_ok=False))
        dispatch.assert_not_called()
        assert body["container_status"] == "stopped"
        assert body["parked"] is True
        assert body["lifecycle"] == "parked"
        assert "wake_retry_after_s" not in body

    @pytest.mark.asyncio
    async def test_parked_explicit_wake1_dispatches(self):
        body, dispatch, budget = await _call("1", _patches(_parked_row(), parked=True))
        dispatch.assert_called_once()
        budget.assert_not_awaited()
        assert body["container_status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_error_row_self_heal_ignores_wake0(self):
        row = {"service_name": "s", "status": "error"}
        body, dispatch, _ = await _call("0", _patches(row, parked=False))
        # The self-heal still fires (dispatch called) regardless of wake=0 —
        # but the status is now reported HONESTLY: a retryable error is
        # error/error_retrying, never masked as "provisioning" (Part B).
        dispatch.assert_called_once()
        assert body["container_status"] == "error"
        assert body["container_substatus"] == "error_retrying"

    @pytest.mark.asyncio
    async def test_trial_gate_fires_before_parked_logic(self):
        """A capped owner gets the 402 limit card, never a parked 200."""
        patches = _patches(_parked_row(), parked=True, trial_reason="trial_daily_cap")
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as dispatch:
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=_auth(), wake="0")
        assert exc.value.status_code == 402
        dispatch.assert_not_called()
