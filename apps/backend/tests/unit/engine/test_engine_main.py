"""The engine service's app (`engine_main.py`, engine-service plan Task 8) and the products' callback routes the
engine calls over HTTP (`core.engine.http_hooks` -> `routers/engine_callbacks.py`)."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from core.config import settings
from core.engine import hooks, http_hooks, transport
from core.engine.hooks import ContextAsk, EngineEvent, ToolCall
from core.voice import engine_hooks

BACKEND = Path(__file__).resolve().parents[3]
PRODUCT = "http://product.test"
TOKEN = "engine-test-token"


def test_engine_main_imports_no_product():
    """The engine service loads no Lightbulb Phone or Meetings code, and calls the products over HTTP only."""
    probe = (
        "import sys, engine_main\n"
        "from core.engine import hooks\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('core.voice', 'routers.voice', 'routers.meetings',"
        " 'routers.huddles')))\n"
        "assert not bad, bad\n"
        "assert hooks._registered[1].__module__ == 'core.engine.http_hooks', hooks._registered\n"
    )
    env = {**os.environ, "CLERK_ISSUER": "https://example.clerk.accounts.dev"}
    done = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def test_engine_main_mounts_the_engine_routes():
    import engine_main

    engine_hooks.register()  # importing engine_main registered the HTTP hooks; this test process keeps the products'
    paths = set(engine_main.app.openapi()["paths"])
    assert {
        "/health",
        "/engine/v1/conversations/{conversation_id}/lines",
        "/api/v1/internal/runtime/{owner_id}/runs/{run_id}/events",
        "/api/v1/internal/automations/{owner_id}/voice-start",
        "/api/v1/internal/messaging/agent-messages/{owner_id}",
    } <= paths


@pytest.fixture
async def product(monkeypatch):
    """The engine's hooks pointed at the products' callback routes, in this process."""
    from routers import engine_callbacks

    app = FastAPI()
    app.include_router(engine_callbacks.router, prefix="/api/v1/internal/engine")
    monkeypatch.setattr(settings, "ENGINE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "PRODUCT_CALLBACK_URL", PRODUCT)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=PRODUCT)
    monkeypatch.setitem(transport._clients, PRODUCT, http)
    http_hooks.register()
    yield http
    engine_hooks.register()
    await http.aclose()


async def test_callbacks_round_trip(product, monkeypatch):
    """`hooks.context`, `hooks.tool` and `hooks.event` over HTTP answer as the products' in-process handlers: the
    same answer, the same refusal, the same handler awaited."""
    ask = ContextAsk(tenant_id="org_1", purpose="router")
    assert await hooks.context(ask) == await engine_hooks._context(ask)

    monkeypatch.setitem(engine_hooks._TOOLS, "front_desk", AsyncMock(return_value={"ok": True, "n": 1}))
    assert await hooks.tool("front_desk", ToolCall(tenant_id="org_1", trigger_ids=["t1"])) == {"ok": True, "n": 1}
    refusal = HTTPException(status_code=403, detail={"code": "nobody_available"})
    monkeypatch.setitem(engine_hooks._TOOLS, "transfer_call", AsyncMock(side_effect=refusal))
    with pytest.raises(HTTPException) as refused:
        await hooks.tool("transfer_call", ToolCall(tenant_id="org_1"))
    assert (refused.value.status_code, refused.value.detail) == (403, {"code": "nobody_available"})

    from core.voice.meetings import meeting_notes, records

    moved, line = AsyncMock(), AsyncMock()
    monkeypatch.setattr(meeting_notes, "on_task_moved", moved)
    monkeypatch.setattr(records, "task_moved", line)
    task = {"taskId": "t-1", "attemptCount": 2}
    await hooks.event(EngineEvent(type="task.moved", tenant_id="org_1", task=task, to="idle", announced=True))
    moved.assert_awaited_once_with(task, "idle")
    line.assert_awaited_once_with(task, "idle")


async def test_a_tool_gets_its_own_budget_and_a_timeout_says_it_may_have_run(product, monkeypatch):
    """A tool runs under 15 s (the runtime plugin waits 20 s), sent once; a timeout after it went out reaches the
    agent's tool route as a 504 that says the action may still have completed, not a bare 500."""
    import asyncio

    assert transport.HOOK_BUDGET_S < transport.TOOL_BUDGET_S == transport.TOOL_ATTEMPT_S <= 15

    async def nap(call):
        await asyncio.sleep(1)
        return {}

    slow = AsyncMock(side_effect=nap)
    monkeypatch.setitem(engine_hooks._TOOLS, "meeting_chat", slow)
    monkeypatch.setattr(transport, "TOOL_ATTEMPT_S", 0.2)
    monkeypatch.setattr(transport, "TOOL_BUDGET_S", 0.2)
    with pytest.raises(HTTPException) as timed_out:
        await hooks.tool("meeting_chat", ToolCall(tenant_id="org_1"))
    assert (timed_out.value.status_code, timed_out.value.detail) == (504, http_hooks.TOOL_TIMEOUT)
    assert "may still have completed" in http_hooks.TOOL_TIMEOUT
    slow.assert_awaited_once()


@pytest.mark.parametrize(
    ("exc", "status", "detail"),
    [
        (httpx.ConnectTimeout("x"), 503, http_hooks.TOOL_NOT_SENT),
        (httpx.PoolTimeout("x"), 503, http_hooks.TOOL_NOT_SENT),
        (httpx.ConnectError("x"), 503, http_hooks.TOOL_NOT_SENT),
        (httpx.ReadTimeout("x"), 504, http_hooks.TOOL_TIMEOUT),
        (httpx.WriteTimeout("x"), 504, http_hooks.TOOL_TIMEOUT),
        (TimeoutError(), 504, http_hooks.TOOL_TIMEOUT),
    ],
)
async def test_every_tool_timeout_is_a_clear_answer(monkeypatch, exc, status, detail):
    """Not sent: says nothing was done. Sent or maybe sent: says it may have completed. Never a bare 500."""
    monkeypatch.setattr(transport, "send", AsyncMock(side_effect=exc))
    with pytest.raises(HTTPException) as failed:
        await http_hooks.tool("front_desk", ToolCall(tenant_id="org_1"))
    assert (failed.value.status_code, failed.value.detail) == (status, detail)


async def test_a_failed_context_raises(product, monkeypatch):
    """A product's failure reaches the engine as a failure (an activity retries it), never an empty answer."""
    monkeypatch.setattr(engine_hooks, "_context", AsyncMock(side_effect=RuntimeError("profile store down")))
    with pytest.raises(httpx.HTTPStatusError) as failed:
        await hooks.context(ContextAsk(tenant_id="org_1", purpose="router"))
    assert failed.value.response.status_code == 500


async def test_callbacks_refuse_a_bad_token(product, monkeypatch):
    r = await product.post("/api/v1/internal/engine/context", json={"tenantId": "org_1", "purpose": "router"})
    assert r.status_code == 401
    monkeypatch.setattr(settings, "ENGINE_SERVICE_TOKEN", "")
    r = await product.post(
        "/api/v1/internal/engine/context",
        json={"tenantId": "org_1", "purpose": "router"},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


def test_main_serves_the_engine_callbacks_and_not_the_runtime_routes():
    """The backend serves the three engine callbacks; the runtime's callbacks (runtime, automations, messaging
    internal) are mounted by the engine service alone."""
    import main

    paths = set(main.app.openapi()["paths"])
    assert {
        "/api/v1/internal/engine/tools/{name}",
        "/api/v1/internal/engine/context",
        "/api/v1/internal/engine/events",
    } <= paths
    assert (
        not {
            "/api/v1/internal/messaging/agent-messages/{owner_id}",
            "/api/v1/internal/messaging/runs/{owner_id}/events",
            "/api/v1/internal/agents/voice-profile/{owner_id}",
            "/api/v1/internal/messaging/attachments/{owner_id}/{room_id}/{attachment_id}",
            "/api/v1/internal/runtime/{owner_id}/runs/{run_id}/events",
            "/api/v1/internal/automations/{owner_id}/voice-start",
        }
        & paths
    )
