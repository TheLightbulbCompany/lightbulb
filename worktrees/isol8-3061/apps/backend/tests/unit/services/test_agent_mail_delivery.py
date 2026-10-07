"""agent_mail_delivery: POST inbound mail to the owner's AgentCore runtime, or stay
silent when there is no runtime or the owner is over cap."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_delivery as delivery  # noqa: E402
from core.services import runtime_transport  # noqa: E402
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S  # noqa: E402

_ROW = {"gateway_token": "gw_abc", "agentcore_runtime_arn": "arn:x"}


@pytest.fixture
def row(monkeypatch):
    """The owner's runtime row, under cap by default."""
    state = {"row": dict(_ROW), "cap_reason": None}
    monkeypatch.setattr(delivery.container_repo, "get_by_owner_id", AsyncMock(side_effect=lambda _o: state["row"]))
    monkeypatch.setattr(delivery, "trial_block_reason", AsyncMock(side_effect=lambda _o: state["cap_reason"]))
    return state


async def _call_deliver(**kwargs):
    return await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1", **kwargs
    )


@pytest.mark.asyncio
async def test_delivers_through_the_inbound_door_as_agent_mail(row, monkeypatch):
    """Mail joins the one inbound door the bridge serves, tagged with its own eventKind.
    There is no OpenClaw ``/hooks/agent`` route and no ``deliver: False`` -- that only ever
    existed to silence OpenClaw's channel-outbound layer, which the bridge doesn't have."""
    captured = {}

    async def fake_request(container, path, *, json_body=None, headers=None, timeout_s=30.0, **_):
        captured.update(container=container, path=path, json_body=json_body, headers=headers, timeout_s=timeout_s)
        return runtime_transport.RuntimeResponse(202, '{"eventId": "eml_1", "status": "accepted"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    assert await _call_deliver() == "delivered"
    assert captured["container"] is row["row"]
    assert captured["path"] == "/plugins/lightbulb-channel/inbound/agent_friday"
    # The bridge's inbound door is gated on the GATEWAY token (server.py's _authorized),
    # never the hook token -- final-review C2.
    assert captured["headers"] == {"Authorization": "Bearer gw_abc"}
    # No session_key argument -> OMITTED entirely, never a literal null.
    assert captured["json_body"] == {
        "version": 1,
        "eventId": "eml_1",
        "eventKind": "agent_mail",
        "targetAgentId": "agent_friday",
        "message": {"text": "hi"},
    }
    # AgentCore holds the invoke through a cold microVM start -- the same 90s budget the
    # canonical ingress caller (openclaw_work._post) uses, not the old ECS-era 10s.
    assert captured["timeout_s"] == _AGENTCORE_INGRESS_TIMEOUT_S == 90.0


@pytest.mark.asyncio
async def test_session_key_is_forwarded_and_a_replayed_200_still_delivers(row, monkeypatch):
    """A 200 ``replayed`` answer (the ingress already ran for this eventId) is just as much
    a delivery as a fresh 202 ``accepted``."""
    captured = {}

    async def fake_request(container, path, *, json_body=None, **_):
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(200, '{"eventId": "eml_1", "status": "replayed"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    assert await _call_deliver(session_key="lightbulb-email:thread-42") == "delivered"
    assert captured["json_body"]["sessionKey"] == "lightbulb-email:thread-42"


@pytest.mark.asyncio
async def test_over_cap_never_invokes_the_runtime(row, monkeypatch):
    """A cold start is cost, so it stays fenced exactly as chat is."""
    row["cap_reason"] = "trial_daily_cap"

    async def boom(*a, **k):
        raise AssertionError("an over-cap owner must not be invoked")

    monkeypatch.setattr(runtime_transport, "request", boom)

    assert await _call_deliver() == "container_not_running"


@pytest.mark.asyncio
async def test_no_runtime_row_is_not_running(row, monkeypatch):
    row["row"] = None

    async def boom(*a, **k):
        raise AssertionError("there is nothing to invoke")

    monkeypatch.setattr(runtime_transport, "request", boom)

    assert await _call_deliver() == "container_not_running"


@pytest.mark.asyncio
async def test_a_row_without_a_runtime_arn_is_not_running(row, monkeypatch):
    row["row"] = {"gateway_token": "gw_abc"}

    async def boom(*a, **k):
        raise AssertionError("there is nothing to invoke")

    monkeypatch.setattr(runtime_transport, "request", boom)

    assert await _call_deliver() == "container_not_running"


@pytest.mark.asyncio
async def test_transport_failure_never_raises(row, monkeypatch):
    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(runtime_transport, "request", boom)

    assert await _call_deliver() == "error"


@pytest.mark.asyncio
async def test_non_2xx_response_is_not_reported_delivered(row, monkeypatch):
    """A 4xx/5xx from the inbound door must resolve to "error", not "delivered" -- otherwise
    a failed delivery is silently reported as a success and SES never retries."""

    async def fake_request(*a, **k):
        return runtime_transport.RuntimeResponse(500, '{"error": "boom"}', {})

    monkeypatch.setattr(runtime_transport, "request", fake_request)

    assert await _call_deliver() == "error"
