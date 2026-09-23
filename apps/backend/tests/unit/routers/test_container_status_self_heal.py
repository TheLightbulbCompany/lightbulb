"""GET /container/status auto-retry matrix.

A broken workspace re-provisions itself off the readiness poll, but a gate 402s
first and a failed provision is reported honestly rather than masked as
"provisioning". There is no wake to suppress any more: AWS parks an idle
microVM invisibly and wakes it on the next invoke, so an idle workspace's row
stays "running" and never reaches this branch at all.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from routers.container import container_status


def _auth(user_id: str = "user_w") -> AuthContext:
    return AuthContext(user_id=user_id, org_id="org_w", org_role="org:admin", email="w@example.com")


async def _compute_status(_owner_id: str, *, container: dict, onboarding: dict) -> dict:
    return {
        "lifecycle": "error" if container.get("status") == "error" else "provisioning",
        "runtime_phase": "provisioning",
        "chat_available": False,
        "container_status": container.get("status"),
        "container_substatus": container.get("substatus"),
        "parked": False,
        "provider": {"subscription_status": "active"},
        "teams_state": "ready",
    }


def _patches(row: dict, *, trial_reason=None, dispatch=None):
    """The full patch stack for driving the auto-retry branch. ensure_user_ready runs its
    REAL dispatch decision (``_provision_or_reconcile``) against the SAME dispatch mock the
    router could call, so a test counts every dispatch one poll makes."""
    from core.services import user_provision

    dispatch = dispatch or AsyncMock()

    async def _ensure_user_ready(auth):
        await user_provision._provision_or_reconcile(auth, "org_w")

    return [
        patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
        patch("routers.container.maybe_restore_trial_bedrock", new=AsyncMock()),
        patch("routers.container.maybe_resume_stuck_conversion", new=AsyncMock(return_value=False)),
        patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=trial_reason)),
        patch("routers.container._assert_provision_allowed", new=AsyncMock()),
        patch("routers.container.dispatch_provision", new=dispatch),
        patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()),
        patch("routers.container.ensure_user_ready", new=_ensure_user_ready),
        patch("routers.container.readiness_state.resolve_onboarding_state", new=AsyncMock(return_value={})),
        patch("routers.container.readiness_state.compute_status", new=_compute_status),
        patch.object(user_provision, "dispatch_provision", new=dispatch),
        patch.object(user_provision.wake_policy, "is_parked_trial", new=AsyncMock(return_value=False)),
        patch.object(user_provision, "_reconcile_owner_seed", new=AsyncMock()),
    ]


async def _call(patches):
    """Run one poll and drain every task it spawned (nested ones included)."""
    pending: list = []
    with ExitStack() as stack:
        entered = [stack.enter_context(item) for item in patches]
        stack.enter_context(patch("asyncio.create_task", side_effect=pending.append))
        body = await container_status(auth=_auth())
        while pending:
            await pending.pop(0)
        return body, entered[5]


class TestAutoRetry:
    @pytest.mark.asyncio
    async def test_a_stopped_row_self_heals(self):
        body, dispatch = await _call(_patches({"status": "stopped"}))
        dispatch.assert_called_once()
        assert body["container_status"] == "provisioning"
        assert body["container_substatus"] == "auto_retry"

    @pytest.mark.asyncio
    async def test_a_retryable_error_row_is_reported_honestly_while_it_retries(self):
        """Never mask a failed provision as "provisioning" -- the client shows
        "hit a problem, retrying" instead of an indefinite spinner."""
        body, dispatch = await _call(_patches({"status": "error"}))
        dispatch.assert_called_once()
        assert body["container_status"] == "error"
        assert body["container_substatus"] == "error_retrying"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row", [{"status": "error", "retryable": True}, {"status": "stopped"}])
    async def test_one_poll_dispatches_exactly_once(self, row):
        """The router shapes the response; ensure_user_ready is the ONE dispatcher."""
        _body, dispatch = await _call(_patches(row))
        assert dispatch.await_count == 1

    @pytest.mark.asyncio
    async def test_a_non_retryable_error_row_is_not_re_dispatched(self):
        body, dispatch = await _call(_patches({"status": "error", "retryable": False}))
        dispatch.assert_not_called()
        assert body["container_status"] == "error"

    @pytest.mark.asyncio
    async def test_a_running_row_never_enters_the_branch(self):
        body, dispatch = await _call(_patches({"status": "running"}))
        dispatch.assert_not_called()
        assert body["container_status"] == "running"

    @pytest.mark.asyncio
    async def test_trial_gate_fires_before_the_self_heal(self):
        """A capped owner gets the 402 limit card, never a silent re-provision."""
        patches = _patches({"status": "stopped"}, trial_reason="trial_daily_cap")
        with ExitStack() as stack:
            entered = [stack.enter_context(item) for item in patches]
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=_auth())
        assert exc.value.status_code == 402
        entered[5].assert_not_called()
