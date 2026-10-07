import asyncio

import pytest

from core.services.ops import envelope as env


def test_envelope_shape_ok():
    e = env.envelope({"running": 3}, freshness_ts="2026-06-30T00:00:00Z")
    assert e["data"] == {"running": 3}
    assert e["source_state"] == "ok"
    assert e["errors"] == []
    assert e["freshness_ts"] == "2026-06-30T00:00:00Z"


@pytest.mark.asyncio
async def test_read_source_success():
    async def ok():
        return {"n": 1}

    data, state, errors = await env.read_source("ecs", ok)
    assert data == {"n": 1}
    assert state == "ok"
    assert errors == []


@pytest.mark.asyncio
async def test_read_source_swallows_exception_as_unavailable():
    async def boom():
        raise RuntimeError("throttled")

    data, state, errors = await env.read_source("ecs", boom)
    assert data is None
    assert state == "unavailable"
    assert errors and errors[0]["domain"] == "ecs"
    assert "throttled" in errors[0]["reason"]


@pytest.mark.asyncio
async def test_read_source_timeout():
    async def slow():
        await asyncio.sleep(1)
        return {"n": 1}

    data, state, errors = await env.read_source("ecs", slow, timeout_s=0.01)
    assert data is None
    assert state == "unavailable"
    assert errors and errors[0]["domain"] == "ecs"
