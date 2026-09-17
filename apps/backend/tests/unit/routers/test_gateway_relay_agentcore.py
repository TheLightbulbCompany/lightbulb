"""Task 26: `gateway_relay._serve_node_protocol` terminates the OpenClaw node protocol in
the backend itself for an AgentCore workspace -- no second socket, no container to dial.

These tests call `_serve_node_protocol` directly against a fake WebSocket (no real ASGI
upgrade, no real container, no DynamoDB) -- `relay_hooks.on_node_ready`/`on_node_closed`
are patched out so the router-level test stays isolated from relay_hooks' own (DB-backed)
AgentCore guard; that guard is covered separately in test_relay_hooks_agentcore.py.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from core.services import node_broker, runtime_transport
from core.services.machine_auth import MachineIdentity
from routers import gateway_relay


class _FakeSocket:
    """Minimal fake of the starlette WebSocket surface `_serve_node_protocol` uses.
    `receive_text` raises WebSocketDisconnect once `received` is exhausted -- the same
    exception the real handshake's `except (asyncio.TimeoutError, WebSocketDisconnect)`
    already treats as "no connect landed in time", so an empty queue doubles as both "the
    client hung up" (post-handshake) and "the deadline passed" (pre-handshake) without a
    real clock in the test.
    """

    def __init__(self, received: list[str], sent: list[str]):
        self._received = list(received)
        self.sent = sent

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        if not self._received:
            raise WebSocketDisconnect(code=1000)
        return self._received.pop(0)


def _identity(**overrides) -> MachineIdentity:
    fields = {
        "user_id": "user_1",
        "org_id": "org_1",
        "owner_id": "org_1",
        "kind": "node",
        "auth_kind": "device",
        "client_id": "node-host",
        "device_id": "dev-1",
    }
    fields.update(overrides)
    return MachineIdentity(**fields)


def _connect_frame(req_id: str, commands: list[str]) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id,
            "method": "connect",
            "params": {
                "minProtocol": 4,
                "maxProtocol": 4,
                "role": "node",
                "client": {"id": "node-host", "mode": "node", "platform": "macos"},
                "scopes": [],
                "commands": commands,
                "auth": {},
                "device": {"id": "dev-1"},
            },
        }
    )


class _CountingRouterSocket:
    """Fake used to call `gateway_relay_ws` directly (the real router coroutine, not
    `_serve_node_protocol`) -- models the one behavior fix round 1 item 1 is about:
    starlette's real `WebSocket.accept()` raises `RuntimeError` on a SECOND call once the
    connection is already CONNECTED (verified against `starlette.websockets.WebSocket.send`'s
    state machine -- a second `websocket.accept` message is not a legal ASGI send once
    `application_state` is already CONNECTED)."""

    def __init__(self, received: list[str]):
        self.query_params = {"token": "x", "clientId": "node-host"}
        self._received = list(received)
        self.sent: list[str] = []
        self.accept_calls = 0
        self._accepted = False

    async def accept(self) -> None:
        self.accept_calls += 1
        if self._accepted:
            raise RuntimeError(
                'Expected ASGI message "websocket.send" or "websocket.close", but got \'websocket.accept\''
            )
        self._accepted = True

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        if not self._received:
            raise WebSocketDisconnect(code=1000)
        return self._received.pop(0)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        return None


@pytest.fixture(autouse=True)
def _hooks_are_noise_here():
    """relay_hooks.on_node_ready/on_node_closed are relay_hooks' own concern (and, for an
    AgentCore owner, hit DynamoDB to decide that -- see test_relay_hooks_agentcore.py).
    Patch them out so this file only asserts on the protocol layer."""
    with (
        patch("routers.gateway_relay.relay_hooks.on_node_ready", new_callable=AsyncMock),
        patch("routers.gateway_relay.relay_hooks.on_node_closed", new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
async def test_router_accepts_the_socket_exactly_once_for_an_agentcore_node():
    """Fix round 1, item 1 (critical): the router used to `accept()` the socket itself
    AND call into `_serve_node_protocol`, which also accepts -- the second accept raised
    RuntimeError before the challenge frame ever went out, closing 1011 and leaving the
    Mac to reconnect forever."""
    fake = _CountingRouterSocket([])
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(
        return_value=({"runtime_host": "agentcore"}, runtime_transport.AGENTCORE_ADDRESS)
    )
    svc = MagicMock()
    svc.put_relay_presence = AsyncMock()
    svc.delete_connection = MagicMock()
    with (
        patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=_identity()),
        patch("routers.gateway_relay.get_ecs_manager", return_value=manager),
        patch("routers.gateway_relay.ConnectionService", return_value=svc),
    ):
        await gateway_relay.gateway_relay_ws(fake)

    assert fake.accept_calls == 1
    assert json.loads(fake.sent[0])["event"] == "connect.challenge"


@pytest.mark.asyncio
async def test_the_relay_speaks_first_and_answers_a_valid_connect_with_hello_ok():
    sent: list[str] = []
    received = [_connect_frame("c-1", ["system.run"])]
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        code = await gateway_relay._serve_node_protocol(
            _FakeSocket(received, sent), _identity(), {"runtime_host": "agentcore"}
        )

    challenge = json.loads(sent[0])
    assert challenge["type"] == "event" and challenge["event"] == "connect.challenge"
    hello = json.loads(sent[1])
    assert hello["id"] == "c-1" and hello["payload"]["type"] == "hello-ok"
    assert gateway_relay.node_sessions == {}  # popped in the function's own finally
    assert code == 1000


@pytest.mark.asyncio
async def test_a_declared_command_outside_the_paired_record_is_dropped_from_the_allowlist():
    seen_commands: dict[str, set[str]] = {}

    async def _capture_ready(owner_id, user_id, device_id):
        # Snapshot the session's commands while it is still registered -- on_node_ready
        # fires after the session is stored and before the socket is ever pumped/closed.
        seen_commands[owner_id] = set(gateway_relay.node_sessions[owner_id].commands)

    received = [_connect_frame("c-2", ["system.run", "camera.capture"])]
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
        patch("routers.gateway_relay.relay_hooks.on_node_ready", new_callable=AsyncMock, side_effect=_capture_ready),
    ):
        await gateway_relay._serve_node_protocol(_FakeSocket(received, []), _identity(), {"runtime_host": "agentcore"})

    # The declared set is intersected with the paired record: a client that declares more
    # than the owner approved gets the intersection, never the union.
    assert seen_commands["org_1"] == {"system.run"}


@pytest.mark.asyncio
async def test_a_missing_connect_inside_the_deadline_closes_1008():
    with patch.object(node_broker, "CHALLENGE_DEADLINE_S", 0.01):
        code = await gateway_relay._serve_node_protocol(_FakeSocket([], []), _identity(), {"runtime_host": "agentcore"})
    assert code == 1008
    assert gateway_relay.node_sessions == {}


@pytest.mark.asyncio
async def test_a_bad_signature_closes_1008_without_registering_a_session():
    received = [_connect_frame("c-3", ["system.run"])]
    with patch.object(node_broker, "signature_matches", lambda params, nonce: False):
        code = await gateway_relay._serve_node_protocol(
            _FakeSocket(received, []), _identity(), {"runtime_host": "agentcore"}
        )
    assert code == 1008
    assert gateway_relay.node_sessions == {}
