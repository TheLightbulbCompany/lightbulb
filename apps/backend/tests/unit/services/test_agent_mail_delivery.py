"""agent_mail_delivery: POST to the container /hooks/agent, or silent when down."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_delivery as delivery  # noqa: E402


def _fake_ecs(container, ip):
    m = AsyncMock()
    m.resolve_running_container = AsyncMock(return_value=(container, ip))
    return m


@pytest.mark.asyncio
async def test_delivers_to_running_container(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "runId": "run_1"})

    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "tok_abc"}, "10.0.0.5"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "delivered"
    assert "10.0.0.5" in captured["url"]
    assert captured["url"].endswith("/hooks/agent")
    assert captured["auth"] == "Bearer tok_abc"
    import json as _j

    body = _j.loads(captured["json"])
    assert body["message"] == "hi"
    assert body["agentId"] == "agent_friday"
    assert "sessionKey" not in body


@pytest.mark.asyncio
async def test_silent_when_container_not_running(monkeypatch):
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs(None, None))
    posted = {"called": False}

    def handler(request):
        posted["called"] = True
        return httpx.Response(200)

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "container_not_running"
    assert posted["called"] is False  # never POSTed, never woke


@pytest.mark.asyncio
async def test_error_on_post_failure_never_raises(monkeypatch):
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))

    def boom(r):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "error"
