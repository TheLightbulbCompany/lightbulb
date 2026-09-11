"""Tests for cold-start observability in container_provision."""

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _make_timing_mock():
    """Build a MagicMock that also works as a context manager for `with timing(...):`."""

    @contextmanager
    def fake_cm(*_args, **_kwargs):
        yield

    m = MagicMock(side_effect=fake_cm)
    return m


@pytest.mark.asyncio
async def test_cold_start_emits_count_and_latency_on_success():
    """When a stopped container is restarted, the route emits both
    gateway.cold_start.count (outcome=ok) and a gateway.cold_start.latency
    timing wrapper around the start_user_service call."""
    from routers.container import container_provision

    auth = MagicMock()
    auth.user_id = "user_returning"
    auth.org_id = "org_returning"

    fake_ecs = MagicMock()
    fake_ecs.start_user_service = AsyncMock()

    fake_timing = _make_timing_mock()

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"status": "stopped", "service_name": "openclaw-foo"},
        ),
        patch("routers.container.get_ecs_manager", return_value=fake_ecs),
        patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()),
        patch("routers.container.put_metric") as mock_put_metric,
        patch("routers.container.timing", fake_timing),
    ):
        result = await container_provision(auth=auth)

    assert result["status"] == "provisioning"

    # `timing()` was used to wrap start_user_service.
    fake_timing.assert_called_once_with("gateway.cold_start.latency")
    # `put_metric()` was called with cold_start.count + outcome=ok.
    count_calls = [c for c in mock_put_metric.call_args_list if c.args and c.args[0] == "gateway.cold_start.count"]
    assert any(c.kwargs.get("dimensions", {}).get("outcome") == "ok" for c in count_calls)


@pytest.mark.asyncio
async def test_cold_start_emits_error_outcome_on_failure():
    """If start_user_service raises EcsManagerError, the route emits
    gateway.cold_start.count with outcome=error and re-raises HTTPException."""
    from fastapi import HTTPException

    from core.containers.ecs_manager import EcsManagerError
    from routers.container import container_provision

    auth = MagicMock()
    auth.user_id = "user_returning"
    auth.org_id = "org_returning"

    fake_ecs = MagicMock()
    fake_ecs.start_user_service = AsyncMock(side_effect=EcsManagerError("ECS down"))

    with (
        patch(
            "routers.container.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"status": "stopped", "service_name": "openclaw-foo"},
        ),
        patch("routers.container.get_ecs_manager", return_value=fake_ecs),
        patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()),
        patch("routers.container.put_metric") as mock_put_metric,
        patch("routers.container.timing", _make_timing_mock()),
    ):
        with pytest.raises(HTTPException):
            await container_provision(auth=auth)

    count_calls = [c for c in mock_put_metric.call_args_list if c.args and c.args[0] == "gateway.cold_start.count"]
    assert any(c.kwargs.get("dimensions", {}).get("outcome") == "error" for c in count_calls)
