"""Task 28: `core.services.messaging.invoke_node` -- local invoke, multi-address cross-instance
forwarding, and the node error-code -> HTTP status mapping (M2-R3/R5/R6/R11/R20/R23)."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import messaging as service
from core.services import node_broker
from core.services.connection_service import ConnectionService
from routers import gateway_relay

OWNER = "org_1"
BODY = {
    "command": "system.run",
    "paramsJSON": json.dumps({"command": ["bash", "-lc", "ls"]}),
    "timeoutMs": 120_000,
}


@pytest.fixture(autouse=True)
def _clean_sessions():
    """node_sessions is process-global module state (see test_gateway_relay_agentcore.py)."""
    gateway_relay.node_sessions.pop((OWNER, "dev-1"), None)
    yield
    gateway_relay.node_sessions.pop((OWNER, "dev-1"), None)


@pytest.fixture(autouse=True)
def _stub_own_address(monkeypatch):
    """invoke_node's remote fanout excludes this task's own address -- stub it to a value none
    of this file's presence rows use, so no test depends on (or blocks on) real DNS resolution."""
    monkeypatch.setattr(service.task_address, "own_address", lambda: "http://10.0.0.1:8000")


@pytest.mark.asyncio
async def test_invoke_node_local_session_answers_without_any_forward(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.node_sessions[(OWNER, "dev-1")] = session
    monkeypatch.setattr(session, "invoke", AsyncMock(return_value={"ok": True, "payload": {"stdout": "x"}}))
    forward = AsyncMock(side_effect=AssertionError("should not forward when a local session answers"))
    monkeypatch.setattr(service, "_forward_node_invoke", forward)
    result = await service.invoke_node(OWNER, BODY, forwarded=False)
    assert result == {"ok": True, "payload": {"stdout": "x"}}
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_node_no_local_session_forwards_to_the_presence_address(monkeypatch):
    monkeypatch.setattr(
        ConnectionService,
        "find_node_presences",
        AsyncMock(return_value=[{"taskAddress": "http://10.0.1.7:8000", "connectionId": "relay#1"}]),
    )
    seen = {}

    async def _forward(address, owner_id, body, timeout_ms):
        seen["address"], seen["owner_id"] = address, owner_id
        return {"ok": True, "payload": None}

    monkeypatch.setattr(service, "_forward_node_invoke", _forward)
    result = await service.invoke_node(OWNER, BODY, forwarded=False)
    assert result == {"ok": True, "payload": None}
    assert seen == {"address": "http://10.0.1.7:8000", "owner_id": OWNER}


@pytest.mark.asyncio
async def test_invoke_node_forwarded_call_with_no_local_session_409s_instead_of_bouncing(monkeypatch):
    forward = AsyncMock(side_effect=AssertionError("a forwarded call must never forward again"))
    monkeypatch.setattr(service, "_forward_node_invoke", forward)
    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=True)
    assert exc.value.status_code == 409
    assert exc.value.error.code == "NODE_NOT_ATTACHED"
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_node_no_node_anywhere_is_409(monkeypatch):
    monkeypatch.setattr(ConnectionService, "find_node_presences", AsyncMock(return_value=[]))
    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=False)
    assert exc.value.status_code == 409
    assert exc.value.error.code == "NODE_NOT_ATTACHED"


# ---------------------------------------------------------------------------
# M2-R23 cross-task blind spot: a paired Mac opens TWO node sockets with
# disjoint command sets (openclaw-macos device client vs the CLI node-host;
# only the CLI declares system.run), and with 2-4 backend tasks and no
# stickiness they routinely land on different tasks. These cover the four
# scenarios from the fix-round-1 ruling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_node_local_wrong_device_forwards_to_the_remote_right_device(monkeypatch):
    """Local task holds only the wrong device (declares device.info, not system.run); the
    device that DOES declare it is attached to a different task."""
    gateway_relay.node_sessions[(OWNER, "dev-1")] = node_broker.NodeSession("dev-1", {"device.info"})
    monkeypatch.setattr(
        ConnectionService,
        "find_node_presences",
        AsyncMock(return_value=[{"taskAddress": "http://10.0.1.8:8000", "connectionId": "relay#2"}]),
    )
    forward = AsyncMock(return_value={"ok": True, "payload": {"stdout": "x"}})
    monkeypatch.setattr(service, "_forward_node_invoke", forward)

    result = await service.invoke_node(OWNER, BODY, forwarded=False)

    assert result == {"ok": True, "payload": {"stdout": "x"}}
    forward.assert_awaited_once_with("http://10.0.1.8:8000", OWNER, BODY, 120_000)


@pytest.mark.asyncio
async def test_invoke_node_local_wrong_device_with_no_remote_is_403(monkeypatch):
    gateway_relay.node_sessions[(OWNER, "dev-1")] = node_broker.NodeSession("dev-1", {"device.info"})
    monkeypatch.setattr(ConnectionService, "find_node_presences", AsyncMock(return_value=[]))

    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=False)

    assert exc.value.status_code == 403
    assert exc.value.error.code == "COMMAND_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_invoke_node_no_local_tries_each_remote_in_order_until_one_answers(monkeypatch):
    monkeypatch.setattr(
        ConnectionService,
        "find_node_presences",
        AsyncMock(
            return_value=[
                {"taskAddress": "http://10.0.1.7:8000", "connectionId": "relay#1"},
                {"taskAddress": "http://10.0.1.8:8000", "connectionId": "relay#2"},
            ]
        ),
    )
    calls = []

    async def _forward(address, owner_id, body, timeout_ms):
        calls.append(address)
        if address == "http://10.0.1.7:8000":
            raise service.ApiProblem("NODE_NOT_ATTACHED", "gone")
        return {"ok": True, "payload": {"stdout": "x"}}

    monkeypatch.setattr(service, "_forward_node_invoke", _forward)

    result = await service.invoke_node(OWNER, BODY, forwarded=False)

    assert result == {"ok": True, "payload": {"stdout": "x"}}
    assert calls == ["http://10.0.1.7:8000", "http://10.0.1.8:8000"]


@pytest.mark.asyncio
async def test_invoke_node_forwarded_with_local_wrong_device_is_403_without_forwarding(monkeypatch):
    gateway_relay.node_sessions[(OWNER, "dev-1")] = node_broker.NodeSession("dev-1", {"device.info"})
    presences = AsyncMock(side_effect=AssertionError("a forwarded call must never forward again"))
    monkeypatch.setattr(ConnectionService, "find_node_presences", presences)

    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=True)

    assert exc.value.status_code == 403
    assert exc.value.error.code == "COMMAND_NOT_ALLOWED"
    presences.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_node_timeout_becomes_504(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.node_sessions[(OWNER, "dev-1")] = session
    monkeypatch.setattr(session, "invoke", AsyncMock(side_effect=node_broker.NodeInvokeTimeout("system.run")))
    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=False)
    assert exc.value.status_code == 504
    assert exc.value.error.code == "NODE_TIMEOUT"


@pytest.mark.asyncio
async def test_invoke_node_detached_becomes_409(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.node_sessions[(OWNER, "dev-1")] = session
    monkeypatch.setattr(session, "invoke", AsyncMock(side_effect=node_broker.NodeDetached("system.run")))
    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, BODY, forwarded=False)
    assert exc.value.status_code == 409
    assert exc.value.error.code == "NODE_NOT_ATTACHED"


@pytest.mark.asyncio
async def test_invoke_node_invalid_params_json_is_400(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.node_sessions[(OWNER, "dev-1")] = session
    with pytest.raises(service.ApiProblem) as exc:
        await service.invoke_node(OWNER, {**BODY, "paramsJSON": "{not json"}, forwarded=False)
    assert exc.value.status_code == 400
    assert exc.value.error.code == "INVALID_REQUEST"


def _fake_client(response=None, raises=None, sent=None, client_kwargs=None):
    class _Resp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **kw):
            if client_kwargs is not None:
                client_kwargs.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if sent is not None:
                sent.update(url=url, json=json, headers=headers)
            if raises is not None:
                raise raises
            return _Resp(*response)

    return _Client


@pytest.mark.asyncio
async def test_forward_relays_a_peer_error_verbatim(monkeypatch):
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    monkeypatch.setattr(
        service.httpx, "AsyncClient", _fake_client(response=(409, {"code": "NODE_NOT_ATTACHED", "message": "gone"}))
    )
    with pytest.raises(service.ApiProblem) as exc:
        await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 120_000)
    assert exc.value.status_code == 409
    assert exc.value.error.code == "NODE_NOT_ATTACHED"


@pytest.mark.asyncio
async def test_forward_success_returns_the_peer_body_verbatim(monkeypatch):
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    monkeypatch.setattr(
        service.httpx, "AsyncClient", _fake_client(response=(200, {"ok": True, "payload": {"stdout": "x"}}))
    )
    result = await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 120_000)
    assert result == {"ok": True, "payload": {"stdout": "x"}}


@pytest.mark.asyncio
async def test_forward_transport_failure_becomes_node_not_attached(monkeypatch):
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    monkeypatch.setattr(service.httpx, "AsyncClient", _fake_client(raises=httpx.ConnectError("refused")))
    with pytest.raises(service.ApiProblem) as exc:
        await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 120_000)
    assert exc.value.status_code == 409
    assert exc.value.error.code == "NODE_NOT_ATTACHED"


@pytest.mark.asyncio
async def test_forward_sends_the_hook_token_and_forwarded_header(monkeypatch):
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    sent: dict = {}
    monkeypatch.setattr(
        service.httpx, "AsyncClient", _fake_client(response=(200, {"ok": True, "payload": None}), sent=sent)
    )
    await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 120_000)
    assert sent["url"] == f"http://10.0.1.7:8000/api/v1/internal/node/invoke/{OWNER}"
    assert sent["json"] == BODY
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["headers"][service.NODE_FORWARD_HEADER] == "1"


@pytest.mark.asyncio
async def test_forward_read_timeout_covers_the_clamped_invoke_timeout(monkeypatch):
    """A forwarded system.run can legitimately run 30-180s -- the forward's own httpx
    timeout must not be a flat value shorter than that (fix round 1, item 1)."""
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    client_kwargs: dict = {}
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        _fake_client(response=(200, {"ok": True, "payload": None}), client_kwargs=client_kwargs),
    )
    await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 30_000)
    timeout = client_kwargs["timeout"]
    assert timeout.read >= 35.0
    assert timeout.connect <= 5.0


@pytest.mark.asyncio
async def test_forward_read_timeout_maps_to_node_timeout_not_node_not_attached(monkeypatch):
    monkeypatch.setattr(service.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "tok"}))
    monkeypatch.setattr(service.httpx, "AsyncClient", _fake_client(raises=httpx.ReadTimeout("timed out")))
    with pytest.raises(service.ApiProblem) as exc:
        await service._forward_node_invoke("http://10.0.1.7:8000", OWNER, BODY, 30_000)
    assert exc.value.status_code == 504
    assert exc.value.error.code == "NODE_TIMEOUT"
