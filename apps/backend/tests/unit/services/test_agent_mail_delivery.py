"""agent_mail_delivery: POST to the container /hooks/agent, wake it if asleep
and under cap, or stay silent when over cap."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_delivery as delivery  # noqa: E402
from core.services import runtime_transport  # noqa: E402


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
    # No session_key argument -> the key is OMITTED entirely, so the container
    # mints its own throwaway `hook:<uuid>` (the pre-threading behaviour, and
    # the right fallback for mail with no usable message-id). Sending an
    # empty/None key instead would be rejected upstream.
    assert "sessionKey" not in body
    # Delivery opt-out. Upstream defaults `deliver` to true and `channel` to
    # "last"; our email is not an OpenClaw channel plugin (the agent replies by
    # invoking lightbulb-mail.sh send), so with it unset EVERY inbound-email run
    # ended status=error "Channel is required" even when the reply was sent —
    # making a real failure indistinguishable from a success.
    assert body["deliver"] is False


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


@pytest.mark.asyncio
async def test_session_key_is_forwarded_when_given(monkeypatch):
    """A stable per-thread key is what turns a reply into a CONTINUATION of the
    same session instead of a cold start.

    It only reaches the agent because the container config sets
    hooks.allowRequestSessionKey=true; upstream rejects a request-supplied key
    otherwise (resolveHookSessionKey, source="request").
    """
    captured = {}

    def handler(request):
        captured["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "runId": "run_1"})

    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    result = await delivery.deliver_to_agent(
        owner_id="org_1",
        agent_id="agent_friday",
        message="hi",
        idempotency_key="eml_1",
        session_key="lightbulb-email:deadbeefdeadbeef",
    )

    assert result == "delivered"
    import json as _j

    body = _j.loads(captured["json"])
    assert body["sessionKey"] == "lightbulb-email:deadbeefdeadbeef"


@pytest.mark.asyncio
async def test_pre_flag_container_rejecting_session_key_retries_unthreaded(monkeypatch):
    """A container older than hooks.allowRequestSessionKey 400s a supplied key.

    Without the fallback this became "error" -> the router's 503 -> SES retry ->
    the same 400 forever: inbound email BROKEN for every container whose
    openclaw.json predates the flag. Config lives on per-owner EFS, so a
    backend deploy does not update them.

    Degrading to the throwaway session is strictly better than failing -- the
    message still reaches the agent, just without thread continuity.
    """
    calls = []

    def handler(request):
        import json as _j

        body = _j.loads(request.read())
        calls.append(body)
        if "sessionKey" in body:
            return httpx.Response(400, json={"ok": False, "error": "sessionKey is disabled for ..."})
        return httpx.Response(200, json={"ok": True, "runId": "run_1"})

    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    result = await delivery.deliver_to_agent(
        owner_id="org_1",
        agent_id="agent_friday",
        message="hi",
        idempotency_key="eml_1",
        session_key="lightbulb-email:deadbeefdeadbeef",
    )

    assert result == "delivered", "must not fail just because the container is older than the flag"
    assert len(calls) == 2, "one threaded attempt, then one unthreaded retry"
    assert "sessionKey" in calls[0]
    assert "sessionKey" not in calls[1]


@pytest.mark.asyncio
async def test_agentcore_row_delivers_through_the_runtime_transport_seam(monkeypatch):
    """An owner on AgentCore never dials an IP -- the request goes through
    ``runtime_transport.request`` instead, and the ECS client is never built."""
    agentcore_container = {
        "hook_token": "tok_abc",
        "runtime_host": "agentcore",
        "agentcore_runtime_arn": "arn:x",
    }
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs(agentcore_container, "agentcore"))

    def _boom_client_factory():
        raise AssertionError("ECS client must not be built for an agentcore row")

    monkeypatch.setattr(delivery, "_client_factory", _boom_client_factory)

    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["container"] = container
        captured["path"] = path
        captured["ip"] = ip
        captured["json_body"] = json_body
        captured["headers"] = headers
        return runtime_transport.RuntimeResponse(200, '{"ok": true, "runId": "run_1"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )

    assert result == "delivered"
    assert captured["container"] is agentcore_container
    assert captured["path"] == "/hooks/agent"
    assert captured["ip"] == "agentcore"
    assert captured["headers"] == {"Authorization": "Bearer tok_abc"}
    assert captured["json_body"]["message"] == "hi"
    assert captured["json_body"]["agentId"] == "agent_friday"


@pytest.mark.asyncio
async def test_a_400_without_a_session_key_is_not_retried(monkeypatch):
    """The retry is scoped to the session-key case. A 400 we did NOT cause by
    supplying a key is a real error and must not be re-sent."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"ok": False, "error": "message required"})

    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )

    assert result == "error"
    assert len(calls) == 1, "no retry when we never supplied a sessionKey"
