"""agent_mail_delivery: POST to the container /hooks/agent, wake it if asleep
and under cap, or stay silent when over cap."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_delivery as delivery  # noqa: E402
from core.services import runtime_transport  # noqa: E402
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S  # noqa: E402


def _fake_ecs(container, ip):
    m = AsyncMock()
    m.resolve_running_container = AsyncMock(return_value=(container, ip))
    return m


# NOTE: `test_delivers_to_running_container` was deleted. It exercised the ECS-only
# `/hooks/agent` POST (hook-token auth, the OpenClaw `message`/`agentId`/`deliver` body
# shape) which is unreachable now that `runtime_transport.host_for` is pinned to
# "agentcore" for every container -- the same "running container -> delivered, correct
# addressing/auth/body" concept is already covered on the AgentCore seam by
# test_agentcore_row_delivers_through_the_inbound_door_as_agent_mail below.


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
    ``trial_block_reason`` would return.

    The container row is AgentCore-shaped (every row is, now that `host_for` is pinned) so
    a running-container delivery goes through `runtime_transport.request`, faked here to
    answer 202 accepted -- the wake-gating logic under test sits above that branch and
    applies identically regardless of runtime host.
    """

    def __init__(self, monkeypatch):
        self.ip = "10.0.0.1"
        self.cap_reason = None
        self.started = False
        self.stamped = False
        container = {
            "hook_token": "tok_abc",
            "gateway_token": "gw_abc",
            "runtime_host": "agentcore",
            "agentcore_runtime_arn": "arn:x",
        }

        async def resolve_running_container(owner_id):
            return container, self.ip

        async def start_user_service(owner_id):
            self.started = True

        async def trial_block_reason(owner_id):
            return self.cap_reason

        async def stamp_email_cold_wake(owner_id):
            self.stamped = True

        async def fake_request(
            container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
        ):
            return runtime_transport.RuntimeResponse(202, '{"ok": true}', {})

        ecs = AsyncMock()
        ecs.resolve_running_container = AsyncMock(side_effect=resolve_running_container)
        ecs.start_user_service = AsyncMock(side_effect=start_user_service)
        monkeypatch.setattr(delivery, "get_ecs_manager", lambda: ecs)
        monkeypatch.setattr(delivery, "trial_block_reason", trial_block_reason)
        monkeypatch.setattr(delivery.container_repo, "stamp_email_cold_wake", stamp_email_cold_wake)
        monkeypatch.setattr(runtime_transport, "request", fake_request)


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


# NOTE: `test_session_key_is_forwarded_when_given` was deleted. It exercised the ECS-only
# `/hooks/agent` body shape, unreachable now (see the note above
# test_agentcore_row_delivers_through_the_inbound_door_as_agent_mail); the same
# session-key-forwarded concept is already covered on the AgentCore seam by
# test_agentcore_row_forwards_the_session_key_and_a_replayed_200_still_delivers below.
#
# NOTE: `test_pre_flag_container_rejecting_session_key_retries_unthreaded` was deleted. It
# covered a compatibility shim specific to the OpenClaw `/hooks/agent` route (retry once
# without `sessionKey` when an older container 400s a request-supplied one). The AgentCore
# inbound door has no such shim -- a non-2xx there is just `"error"` (see the branch in
# `agent_mail_delivery.deliver_to_agent`) -- so the shim, and this test of it, is dead code
# now that every container takes the AgentCore branch.


@pytest.mark.asyncio
async def test_agentcore_row_delivers_through_the_inbound_door_as_agent_mail(monkeypatch):
    """An owner on AgentCore never dials an IP, and never hits the OpenClaw
    ``/hooks/agent`` route either -- Hermes has no hooks ingress, so mail joins
    the one inbound door the bridge serves, tagged with its own eventKind.
    ``deliver: False`` is gone too: it only existed to silence OpenClaw's
    channel-outbound layer, which the bridge doesn't have."""
    agentcore_container = {
        "hook_token": "tok_abc",
        "gateway_token": "gw_abc",
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
        captured["timeout_s"] = timeout_s
        return runtime_transport.RuntimeResponse(202, '{"eventId": "eml_1", "runId": null, "status": "accepted"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )

    assert result == "delivered"
    assert captured["container"] is agentcore_container
    assert captured["path"] == "/plugins/lightbulb-channel/inbound/agent_friday"
    assert captured["ip"] == "agentcore"
    # The bridge's inbound door is gated on the GATEWAY token (server.py's _authorized),
    # never the hook token -- final-review C2.
    assert captured["headers"] == {"Authorization": "Bearer gw_abc"}
    # No session_key argument -> OMITTED entirely (never sent as a literal null), same
    # idiom as the ECS branch's own "no session_key -> key absent" test above.
    assert captured["json_body"] == {
        "version": 1,
        "eventId": "eml_1",
        "eventKind": "agent_mail",
        "targetAgentId": "agent_friday",
        "message": {"text": "hi"},
    }
    assert "sessionKey" not in captured["json_body"]
    # AgentCore holds the invoke through a cold microVM start -- the same 90s budget the
    # canonical ingress caller (openclaw_work._post) uses, not the old ECS-era 10s.
    assert captured["timeout_s"] == _AGENTCORE_INGRESS_TIMEOUT_S == 90.0
    assert "deliver" not in captured["json_body"]


@pytest.mark.asyncio
async def test_agentcore_row_forwards_the_session_key_and_a_replayed_200_still_delivers(monkeypatch):
    """A 200 ``replayed`` answer (the ingress already ran for this eventId) is
    just as much a delivery as a fresh 202 ``accepted``."""
    agentcore_container = {
        "hook_token": "tok_abc",
        "gateway_token": "gw_abc",
        "runtime_host": "agentcore",
        "agentcore_runtime_arn": "arn:x",
    }
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs(agentcore_container, "agentcore"))
    monkeypatch.setattr(delivery, "_client_factory", lambda: (_ for _ in ()).throw(AssertionError("no ECS client")))

    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(200, '{"eventId": "eml_1", "status": "replayed"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    result = await delivery.deliver_to_agent(
        owner_id="org_1",
        agent_id="agent_friday",
        message="hi",
        idempotency_key="eml_1",
        session_key="lightbulb-email:thread-42",
    )

    assert result == "delivered"
    assert captured["json_body"]["sessionKey"] == "lightbulb-email:thread-42"


# NOTE: `test_a_400_without_a_session_key_is_not_retried` was deleted. It guarded the same
# ECS-only `/hooks/agent` retry shim described above -- with the shim itself unreachable
# (every container now takes the AgentCore branch, whose non-2xx handling never retries),
# there is nothing left for this test to prove.
