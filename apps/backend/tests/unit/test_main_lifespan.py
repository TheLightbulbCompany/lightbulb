"""Tests for backend lifespan startup wiring."""

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _no_balance_check(monkeypatch):
    """The gauge loop's OpenRouter balance check would otherwise dial Secrets Manager."""
    monkeypatch.setattr("core.services.openrouter_keys.check_account_balance", AsyncMock())


# NOTE: the four ``_resume_wake_resize_transitions`` tests went with that function. It
# resumed an ECS ``_await_running_transition`` poller for every unclaimed row left at
# status=provisioning by a mid-wake/mid-resize deploy. There is no such poller: AgentCore
# wakes the microVM on the invoke, and a provision's transition is owned in the FOREGROUND by
# scripts/provision_runner.py under the SFN watchdog.


@pytest.mark.asyncio
async def test_running_count_gauge_loop_logs_the_emitting_instance(monkeypatch):
    """The ECS task id is a log field, not a metric dimension -- it is a new
    CloudWatch identity per task per deploy, the same class _DENIED_DIMENSIONS
    refuses for container_id. P12 still binds the plain {env, service} gauge."""
    import main

    monkeypatch.setenv("HOSTNAME", "task-abc123")
    gauge_calls: list[tuple[str, float, dict | None]] = []
    log_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "core.observability.metrics.gauge",
        lambda name, value, dimensions=None: gauge_calls.append((name, value, dimensions)),
    )
    monkeypatch.setattr(main.logger, "info", lambda *a, **kw: log_calls.append((a, kw)))
    monkeypatch.setattr(main.container_repo, "get_by_status", AsyncMock(return_value=[{"owner_id": "o1"}]))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert gauge_calls == [("gateway.running.count", 1, None)]
    assert log_calls == [(("running_count",), {"extra": {"instance": "task-abc123", "count": 1}})]


@pytest.mark.asyncio
async def test_running_count_gauge_loop_falls_back_off_ecs(monkeypatch):
    """No HOSTNAME (local/pytest) must not crash the canary."""
    import main

    monkeypatch.delenv("HOSTNAME", raising=False)
    log_calls = []
    monkeypatch.setattr("core.observability.metrics.gauge", lambda name, value, dimensions=None: None)
    monkeypatch.setattr(main.logger, "info", lambda *a, **kw: log_calls.append((a, kw)))
    monkeypatch.setattr(main.container_repo, "get_by_status", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert log_calls == [(("running_count",), {"extra": {"instance": "local", "count": 0}})]


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
