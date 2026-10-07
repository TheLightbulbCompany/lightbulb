"""Tests for the /admin/health aggregator.

Covers:
- Fleet counts grouped by container status.
- Probe cache (30s TTL — second call returns cached value, no upstream hit).
- Probe timeout / error → {status: "down"} (CEO P2).
- Background-task state mapping (running / stopped / cancelled / unregistered).
- get_system_health composes all sources without raising even when probes
  fail or the recent_errors call fails.
"""

import asyncio
import os
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENVIRONMENT", "dev")


@pytest.fixture(autouse=True)
def reset_probe_cache():
    """Each test gets a fresh probe cache."""
    from core.services import system_health

    system_health._probe_cache["ts"] = 0.0
    system_health._probe_cache["value"] = None
    yield


@pytest.fixture(autouse=True)
def reset_background_tasks():
    from core.services import system_health

    system_health.BACKGROUND_TASKS.clear()
    yield
    system_health.BACKGROUND_TASKS.clear()


@pytest.mark.asyncio
async def test_fleet_counts_delegates_to_live_ecs():
    """_fleet_counts delegates to the fleet grid (ops.fleet), under a 2s bound."""
    from unittest.mock import AsyncMock

    from core.services import system_health

    with patch(
        "core.services.system_health.fleet_ops.fleet_counts_live",
        AsyncMock(return_value={"running": 3, "provisioning": 1, "stopped": 2, "error": 0, "unknown": 0, "total": 6}),
    ):
        counts = await system_health._fleet_counts()

    assert counts == {"running": 3, "provisioning": 1, "stopped": 2, "error": 0, "unknown": 0, "total": 6}


@pytest.mark.asyncio
async def test_fleet_counts_returns_live_shape_faithfully():
    """_fleet_counts passes through whatever the grid yields (incl. the unknown key)."""
    from unittest.mock import AsyncMock

    from core.services import system_health

    with patch(
        "core.services.system_health.fleet_ops.fleet_counts_live",
        AsyncMock(return_value={"running": 2, "provisioning": 0, "stopped": 0, "error": 1, "unknown": 1, "total": 4}),
    ):
        counts = await system_health._fleet_counts()

    assert counts["error"] == 1
    assert counts["unknown"] == 1
    assert counts["total"] == 4


@pytest.mark.asyncio
async def test_probe_cache_returns_cached_value_within_ttl():
    from core.services import system_health

    call_count = {"clerk": 0, "stripe": 0, "ddb": 0}

    async def fake_clerk():
        call_count["clerk"] += 1
        return {"status": "ok", "latency_ms": 10}

    async def fake_stripe():
        call_count["stripe"] += 1
        return {"status": "ok", "latency_ms": 20}

    async def fake_ddb():
        call_count["ddb"] += 1
        return {"status": "ok", "latency_ms": 30}

    with (
        patch("core.services.system_health._probe_clerk", new=fake_clerk),
        patch("core.services.system_health._probe_stripe", new=fake_stripe),
        patch("core.services.system_health._probe_ddb", new=fake_ddb),
    ):
        first = await system_health._all_probes()
        second = await system_health._all_probes()

    assert first == second
    # Each probe called exactly once across two _all_probes() calls.
    assert call_count == {"clerk": 1, "stripe": 1, "ddb": 1}


@pytest.mark.asyncio
async def test_probe_cache_expires_past_ttl():
    from core.services import system_health

    call_count = {"n": 0}

    async def fake_clerk():
        call_count["n"] += 1
        return {"status": "ok"}

    async def fake_other():
        return {"status": "ok"}

    with (
        patch("core.services.system_health._probe_clerk", new=fake_clerk),
        patch("core.services.system_health._probe_stripe", new=fake_other),
        patch("core.services.system_health._probe_ddb", new=fake_other),
    ):
        await system_health._all_probes()
        # Force the cache to look stale.
        system_health._probe_cache["ts"] = time.monotonic() - (system_health._PROBE_CACHE_TTL_S + 1)
        await system_health._all_probes()

    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_all_probes_handles_probe_exception_gracefully():
    from core.services import system_health

    async def good():
        return {"status": "ok"}

    async def bad():
        raise RuntimeError("upstream_blip")

    with (
        patch("core.services.system_health._probe_clerk", new=good),
        patch("core.services.system_health._probe_stripe", new=bad),
        patch("core.services.system_health._probe_ddb", new=good),
    ):
        result = await system_health._all_probes()

    assert result["clerk"]["status"] == "ok"
    assert result["stripe"]["status"] == "down"
    assert "upstream_blip" in result["stripe"]["error"]
    assert result["ddb"]["status"] == "ok"


def test_background_tasks_status_running_and_stopped():
    from core.services import system_health

    async def long_running():
        await asyncio.sleep(60)

    loop = asyncio.new_event_loop()
    try:
        running_task = loop.create_task(long_running())

        async def quick():
            return None

        stopped_task = loop.create_task(quick())
        loop.run_until_complete(asyncio.sleep(0))  # let stopped_task complete

        system_health.BACKGROUND_TASKS["idle_checker"] = running_task
        system_health.BACKGROUND_TASKS["scheduled_worker"] = stopped_task

        status = system_health._background_tasks_status()
        assert status["idle_checker"]["status"] == "running"
        assert status["scheduled_worker"]["status"] == "stopped"
    finally:
        running_task.cancel()
        loop.close()


def test_background_tasks_status_unregistered_when_none():
    from core.services import system_health

    system_health.BACKGROUND_TASKS["never_registered"] = None  # type: ignore[assignment]
    status = system_health._background_tasks_status()
    assert status["never_registered"]["status"] == "unregistered"


@pytest.mark.asyncio
async def test_get_system_health_composes_all_sources():
    from core.services import system_health

    async def fake_probes():
        return {"clerk": {"status": "ok"}, "stripe": {"status": "ok"}, "ddb": {"status": "ok"}}

    async def fake_fleet():
        return {"running": 5, "provisioning": 0, "stopped": 1, "error": 0, "total": 6}

    async def fake_recent_errors(*, hours, limit):
        return [{"timestamp": "2026-04-21", "user_id": "u1", "message": "boom"}]

    with (
        patch("core.services.system_health._all_probes", new=fake_probes),
        patch("core.services.system_health._fleet_counts", new=fake_fleet),
        patch("core.services.system_health.cloudwatch_logs.recent_errors_fleet", new=fake_recent_errors),
    ):
        result = await system_health.get_system_health()

    assert result["upstreams"]["clerk"]["status"] == "ok"
    assert result["fleet"]["total"] == 6
    assert isinstance(result["background_tasks"], dict)
    assert len(result["recent_errors"]) == 1


@pytest.mark.asyncio
async def test_get_system_health_never_raises_even_when_components_fail():
    from core.services import system_health

    async def fake_probes():
        raise RuntimeError("boom_clerk")

    async def fake_fleet():
        raise RuntimeError("boom_ddb")

    async def fake_recent_errors(*, hours, limit):
        raise RuntimeError("boom_cwl")

    with (
        patch("core.services.system_health._all_probes", new=fake_probes),
        patch("core.services.system_health._fleet_counts", new=fake_fleet),
        patch("core.services.system_health.cloudwatch_logs.recent_errors_fleet", new=fake_recent_errors),
    ):
        result = await system_health.get_system_health()

    # All components fell back to empty defaults — no exception leaked.
    assert result["upstreams"] == {}
    assert result["fleet"] == {}
    assert result["recent_errors"] == []


@pytest.mark.asyncio
async def test_probe_stripe_unconfigured_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.STRIPE_SECRET_KEY", "")
    from core.services.system_health import _probe_stripe

    result = await _probe_stripe()
    assert result["status"] == "unconfigured"


def test_ddb_probe_targets_the_shared_prefix_not_a_brand_literal(monkeypatch):
    """Regression: the probe hardcoded `isol8-{env}-admin-actions`, so after the
    table rename it described a table that no longer existed and reported the
    whole DDB dependency as down."""
    import asyncio

    from core.services import system_health

    monkeypatch.setattr(system_health.settings, "DYNAMODB_TABLE_PREFIX", "brand-prod-")
    seen = {}

    class _Client:
        def describe_table(self, TableName):  # noqa: N803 — boto3 kwarg name
            seen["name"] = TableName
            return {}

    monkeypatch.setattr(system_health.boto3, "client", lambda *a, **k: _Client())
    result = asyncio.run(system_health._probe_ddb())

    assert seen["name"] == "brand-prod-admin-actions"
    assert result["status"] == "ok"
