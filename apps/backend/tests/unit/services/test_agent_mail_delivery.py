"""agent_mail_delivery: POST to the container /hooks/agent, wake it if asleep
and under cap, or stay silent when over cap."""

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
async def test_error_on_post_failure_never_raises(monkeypatch):
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))

    def boom(r):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "error"


class _WakeHarness:
    """Fakes the container-resolve/start/stamp/cap seams for the wake-on-inbound
    branch. ``ip=None`` means asleep; ``cap_reason`` mirrors what
    ``trial_block_reason`` would return."""

    def __init__(self, monkeypatch):
        self.ip = "10.0.0.1"
        self.cap_reason = None
        self.started = False
        self.stamped = False

        async def resolve_running_container(owner_id):
            return {"hook_token": "tok_abc"}, self.ip

        async def start_user_service(owner_id):
            self.started = True

        async def trial_block_reason(owner_id):
            return self.cap_reason

        async def stamp_email_cold_wake(owner_id):
            self.stamped = True

        ecs = AsyncMock()
        ecs.resolve_running_container = AsyncMock(side_effect=resolve_running_container)
        ecs.start_user_service = AsyncMock(side_effect=start_user_service)
        monkeypatch.setattr(delivery, "get_ecs_manager", lambda: ecs)
        monkeypatch.setattr(delivery, "trial_block_reason", trial_block_reason)
        monkeypatch.setattr(delivery.container_repo, "stamp_email_cold_wake", stamp_email_cold_wake)

        def handler(request):
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(
            delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )


@pytest.fixture
def delivery_harness(monkeypatch):
    return _WakeHarness(monkeypatch)


async def _call_deliver():
    return await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )


@pytest.mark.asyncio
async def test_running_container_delivers_without_waking(delivery_harness):
    delivery_harness.ip = "10.0.0.1"
    assert await _call_deliver() == "delivered"
    assert delivery_harness.started is False
    assert delivery_harness.stamped is False


@pytest.mark.asyncio
async def test_asleep_and_under_cap_starts_the_service_and_stamps(delivery_harness):
    delivery_harness.ip = None
    delivery_harness.cap_reason = None
    assert await _call_deliver() == "woken"
    assert delivery_harness.started is True
    assert delivery_harness.stamped is True


@pytest.mark.asyncio
async def test_asleep_and_over_cap_does_NOT_wake(delivery_harness):
    """Cost stays fenced exactly as chat is."""
    delivery_harness.ip = None
    delivery_harness.cap_reason = "trial_daily_cap"
    assert await _call_deliver() == "container_not_running"
    assert delivery_harness.started is False
    assert delivery_harness.stamped is False


@pytest.mark.asyncio
async def test_non_2xx_response_is_not_reported_delivered(monkeypatch):
    """A 4xx/5xx from the container's /hooks/agent must resolve to "error", not
    "delivered" — guards the `response.raise_for_status()` call in
    `deliver_to_agent`. If that call is ever deleted, a failed delivery would
    silently be reported as a success.
    """
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "tok_abc"}, "10.0.0.5"))

    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "error"
