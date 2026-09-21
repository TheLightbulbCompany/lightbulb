"""Task 26: `gateway_relay._serve_node_protocol` terminates the OpenClaw node protocol in
the backend itself for an AgentCore workspace -- no second socket, no container to dial.

These tests call `_serve_node_protocol` directly against a fake WebSocket (no real ASGI
upgrade, no real container, no DynamoDB) -- `relay_hooks.on_node_ready`/`on_node_closed`
are patched out so the router-level test stays isolated from relay_hooks' own (DB-backed)
AgentCore guard; that guard is covered separately in test_relay_hooks_agentcore.py.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from core.services import node_broker
from core.services.machine_auth import MachineIdentity
from routers import gateway_relay


async def _poll_until(predicate, *, timeout: float = 1.0, interval: float = 0.005) -> None:
    """Small deterministic wait for tests driving `_serve_node_protocol` concurrently as a
    background task -- there is no other signal for "the handshake has reached point X"."""

    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_wait(), timeout=timeout)


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


class _FeedableSocket:
    """Like `_FakeSocket`, but the incoming queue can be fed live and `receive_text` BLOCKS
    (rather than immediately disconnecting) when nothing has been fed yet -- needed for any
    test that must observe a mid-connection state (a session that stays registered, an
    outbound frame the pump sent) rather than the handshake completing and tearing back down
    within a single scheduling tick. Mirrors test_gateway_relay.py's FakeUpstream."""

    def __init__(self):
        self.sent: list[str] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def feed(self, frame: str | None) -> None:
        self._queue.put_nowait(frame)

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        frame = await self._queue.get()
        if frame is None:
            raise WebSocketDisconnect(code=1000)
        return frame


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
    # node_sessions is process-global module state; a test that hits an early-exit path
    # this file doesn't expect to leak (e.g. an exception before the handshake's own
    # cleanup runs) must not bleed a stale entry into every test after it.
    gateway_relay.node_sessions.clear()


@pytest.mark.asyncio
async def test_router_accepts_the_socket_exactly_once_for_an_agentcore_node():
    """Fix round 1, item 1 (critical): the router used to `accept()` the socket itself
    AND call into `_serve_node_protocol`, which also accepts -- the second accept raised
    RuntimeError before the challenge frame ever went out, closing 1011 and leaving the
    Mac to reconnect forever."""
    fake = _CountingRouterSocket([])
    svc = MagicMock()
    svc.put_relay_presence = AsyncMock()
    svc.delete_connection = MagicMock()
    with (
        patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=_identity()),
        patch(
            "routers.gateway_relay.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:r1"},
        ),
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
    seen_commands: dict[tuple[str, str], set[str]] = {}

    async def _capture_ready(owner_id, user_id):
        # Snapshot the session's commands while it is still registered -- on_node_ready
        # fires after the session is stored and before the socket is ever pumped/closed.
        seen_commands[(owner_id, "dev-1")] = set(gateway_relay.node_sessions[(owner_id, "dev-1")].commands)

    received = [_connect_frame("c-2", ["system.run", "camera.capture"])]
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
        patch("routers.gateway_relay.relay_hooks.on_node_ready", new_callable=AsyncMock, side_effect=_capture_ready),
    ):
        await gateway_relay._serve_node_protocol(_FakeSocket(received, []), _identity(), {"runtime_host": "agentcore"})

    # The declared set is intersected with the paired record: a client that declares more
    # than the owner approved gets the intersection, never the union.
    assert seen_commands[("org_1", "dev-1")] == {"system.run"}


@pytest.mark.asyncio
async def test_two_devices_for_one_owner_coexist_and_closing_one_leaves_the_other():
    """Fix round 1, item 2 (critical): a paired Mac runs TWO node sockets at once -- the
    Tauri device client (clientId=openclaw-macos) and the CLI (clientId=node-host), each a
    separate device id. Keying node_sessions by owner_id alone means the second connect
    silently evicts the first's session; keying by (owner_id, device_id) lets both live."""
    mac_socket, cli_socket = _FeedableSocket(), _FeedableSocket()
    mac_socket.feed(_connect_frame("c-mac", ["device.info"]))
    cli_socket.feed(_connect_frame("c-cli", ["system.run"]))
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(
            gateway_relay.desktop_pair,
            "declared_commands_for",
            AsyncMock(side_effect=lambda owner, device: {"device.info"} if device == "mac-1" else {"system.run"}),
        ),
    ):
        mac_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(mac_socket, _identity(device_id="mac-1"), {"runtime_host": "agentcore"})
        )
        cli_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(cli_socket, _identity(device_id="cli-1"), {"runtime_host": "agentcore"})
        )
        await _poll_until(lambda: ("org_1", "mac-1") in gateway_relay.node_sessions)
        await _poll_until(lambda: ("org_1", "cli-1") in gateway_relay.node_sessions)

        # Both sessions are live at once, with their OWN declared commands -- neither
        # evicted the other.
        assert gateway_relay.node_sessions[("org_1", "mac-1")].commands == {"device.info"}
        assert gateway_relay.node_sessions[("org_1", "cli-1")].commands == {"system.run"}

        # Closing the Mac's socket (a normal client disconnect) must not touch the CLI's
        # still-live session.
        mac_socket.feed(None)
        await asyncio.wait_for(mac_task, timeout=1.0)
        await _poll_until(lambda: ("org_1", "mac-1") not in gateway_relay.node_sessions)
        assert ("org_1", "cli-1") in gateway_relay.node_sessions

        cli_socket.feed(None)
        await asyncio.wait_for(cli_task, timeout=1.0)


@pytest.mark.asyncio
async def test_find_node_session_picks_the_device_that_declared_the_command():
    gateway_relay.node_sessions[("org_1", "mac-1")] = node_broker.NodeSession("mac-1", {"device.info"})
    gateway_relay.node_sessions[("org_1", "cli-1")] = node_broker.NodeSession("cli-1", {"system.run"})
    try:
        found = gateway_relay.find_node_session("org_1", command="system.run")
        assert found is not None and found.node_id == "cli-1"
        assert gateway_relay.find_node_session("org_1", command="camera.capture") is None
        assert gateway_relay.find_node_session("other_org", command="system.run") is None
        assert gateway_relay.find_node_session("org_1") is not None  # no command filter: any live session
    finally:
        gateway_relay.node_sessions.pop(("org_1", "mac-1"), None)
        gateway_relay.node_sessions.pop(("org_1", "cli-1"), None)


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


def _invoke_request_id(sent: list[str]) -> str:
    frame = next(json.loads(f) for f in sent if json.loads(f).get("event") == "node.invoke.request")
    return frame["payload"]["id"]


@pytest.mark.asyncio
async def test_a_result_frame_over_the_old_64kib_parse_limit_still_resolves_the_invoke():
    """Fix round 1, item 3 (critical): the reader used to decode with the same 64 KiB
    _PARSE_LIMIT the ECS inspection peek uses -- a node.invoke.result over that size was
    silently dropped (no ack, the future never resolves, and an unanswered req suppresses
    the CLI's own tick watchdog). The node reader must decode up to node_broker.MAX_PAYLOAD
    (25 MiB)."""
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-4", ["system.run"]))
    big_stdout = "x" * 100_000  # 100 KB: over the old 64 KiB limit, nowhere near MAX_PAYLOAD
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "TICK_INTERVAL_MS", 60_000),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        serve_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(socket, _identity(), {"runtime_host": "agentcore"})
        )
        await _poll_until(lambda: ("org_1", "dev-1") in gateway_relay.node_sessions)
        session = gateway_relay.node_sessions[("org_1", "dev-1")]
        invoke_task = asyncio.create_task(session.invoke("system.run", {}, 5_000))

        await _poll_until(lambda: any(json.loads(f).get("event") == "node.invoke.request" for f in socket.sent))
        invoke_id = _invoke_request_id(socket.sent)

        socket.feed(
            json.dumps(
                {
                    "type": "req",
                    "id": "r-2",
                    "method": "node.invoke.result",
                    "params": {"id": invoke_id, "ok": True, "payload": {"stdout": big_stdout}},
                }
            )
        )
        result = await asyncio.wait_for(invoke_task, timeout=1.0)
        assert result == {"ok": True, "payload": {"stdout": big_stdout}}

        socket.feed(None)
        await asyncio.wait_for(serve_task, timeout=1.0)


@pytest.mark.asyncio
async def test_a_frame_over_max_payload_closes_1009():
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-5", ["system.run"]))
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "MAX_PAYLOAD", 1_000),  # small ceiling so the test frame is trivially "too big"
        patch.object(node_broker, "TICK_INTERVAL_MS", 60_000),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        serve_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(socket, _identity(), {"runtime_host": "agentcore"})
        )
        await _poll_until(lambda: ("org_1", "dev-1") in gateway_relay.node_sessions)
        socket.feed(json.dumps({"type": "req", "id": "r-3", "method": "node.event", "params": {"x": "y" * 2_000}}))
        code = await asyncio.wait_for(serve_task, timeout=1.0)
    assert code == 1009


@pytest.mark.asyncio
async def test_an_in_flight_invoke_is_failed_immediately_when_the_socket_closes():
    """Fix round 1, item 4 (important): a pending invoke used to just sit there until its own
    timeout_ms elapsed when the node's socket closed underneath it -- a caller (Task 28)
    would wait out the full budget for an answer that could never arrive. Closing the socket
    must fail every in-flight invoke immediately with NodeDetached."""
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-6", ["system.run"]))
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "TICK_INTERVAL_MS", 60_000),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        serve_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(socket, _identity(), {"runtime_host": "agentcore"})
        )
        await _poll_until(lambda: ("org_1", "dev-1") in gateway_relay.node_sessions)
        session = gateway_relay.node_sessions[("org_1", "dev-1")]
        # A long timeout_ms -- if fail_pending did not fire, this assertion would need the
        # full 60s to resolve; asyncio.wait_for's 1s bound is what actually proves "immediately".
        invoke_task = asyncio.create_task(session.invoke("system.run", {}, 60_000))
        await _poll_until(lambda: any(json.loads(f).get("event") == "node.invoke.request" for f in socket.sent))

        socket.feed(None)  # the client hangs up while the invoke is still in flight
        await asyncio.wait_for(serve_task, timeout=1.0)

        with pytest.raises(node_broker.NodeDetached):
            await asyncio.wait_for(invoke_task, timeout=1.0)


@pytest.mark.asyncio
async def test_a_leg_exception_closes_1011_and_logs_it(caplog):
    """Fix round 1, item 5 (important): _pump used to only ever return 1000 or 1008 --
    a leg raising (anything other than the frame-too-large / disconnect cases) must still
    close 1011 and log it, the same pattern the ECS branch's own asyncio.wait loop uses."""
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-7", ["system.run"]))
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "TICK_INTERVAL_MS", 5),
        patch.object(node_broker, "tick_frame", side_effect=RuntimeError("boom")),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
        caplog.at_level(logging.WARNING, logger="routers.gateway_relay"),
    ):
        code = await asyncio.wait_for(
            gateway_relay._serve_node_protocol(socket, _identity(), {"runtime_host": "agentcore"}), timeout=1.0
        )
    assert code == 1011
    assert any("relay leg failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancelling_the_serve_task_cancels_and_awaits_every_leg():
    """Fix round 1, item 6 (important): asyncio.wait does not cancel its own awaitables --
    if `_pump`'s own coroutine is cancelled while blocked inside asyncio.wait (a shutdown/
    drain interrupting the connection, NOT a leg finishing on its own), the reader/writer/
    ticker (and refresher, when wired) tasks it created were left running with nothing left
    to ever cancel or await them."""
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-8", ["system.run"]))
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "TICK_INTERVAL_MS", 60_000),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        before = asyncio.all_tasks()
        serve_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(socket, _identity(), {"runtime_host": "agentcore"})
        )
        await _poll_until(lambda: ("org_1", "dev-1") in gateway_relay.node_sessions)
        await asyncio.sleep(0)  # let the reader/writer/ticker legs actually start running

        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task

        await asyncio.sleep(0)  # give any leaked task one more turn to (fail to) wind down
        leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
        assert all(task.done() for task in leaked), [t.get_coro() for t in leaked if not t.done()]


# --- Fix round 1, item 7 (minors) ---


class _SocketThatFailsOnHelloOk(_FakeSocket):
    """Raises on the SECOND send_text call (the hello-ok) -- used to prove the session is
    registered only after that send lands, never before it."""

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        if len(self.sent) == 2:
            raise RuntimeError("socket died right as hello-ok went out")


@pytest.mark.asyncio
async def test_the_session_is_registered_only_after_hello_ok_actually_sends():
    received = [_connect_frame("c-9", ["system.run"])]
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        with pytest.raises(RuntimeError):
            await gateway_relay._serve_node_protocol(
                _SocketThatFailsOnHelloOk(received, []), _identity(), {"runtime_host": "agentcore"}
            )
    # The hello-ok send raised before the registration line ever ran.
    assert gateway_relay.node_sessions == {}


@pytest.mark.asyncio
async def test_ready_generation_is_published_after_hello_and_removed_with_a_generation_fence():
    socket = _FeedableSocket()
    socket.feed(_connect_frame("c-ready", ["system.run"]))
    connections = MagicMock()

    async def _publish(**kwargs):
        assert json.loads(socket.sent[-1])["payload"]["type"] == "hello-ok"

    connections.publish_desktop_ready = AsyncMock(side_effect=_publish)
    connections.refresh_relay_presence = AsyncMock(return_value=True)
    connections.refresh_desktop_ready = AsyncMock(return_value=True)
    connections.delete_desktop_ready = AsyncMock()
    with (
        patch.object(node_broker, "signature_matches", lambda params, nonce: True),
        patch.object(node_broker, "TICK_INTERVAL_MS", 60_000),
        patch.object(gateway_relay.desktop_pair, "declared_commands_for", AsyncMock(return_value={"system.run"})),
    ):
        serve_task = asyncio.create_task(
            gateway_relay._serve_node_protocol(
                socket,
                _identity(),
                {"runtime_host": "agentcore"},
                connections=connections,
                connection_id="relay#ready",
            )
        )
        await _poll_until(lambda: ("org_1", "dev-1") in gateway_relay.node_sessions)
        generation = connections.publish_desktop_ready.await_args.kwargs["generation"]
        assert gateway_relay.node_sessions[("org_1", "dev-1")].generation == generation

        socket.feed(None)
        await asyncio.wait_for(serve_task, timeout=1.0)

    connections.delete_desktop_ready.assert_awaited_once_with("org_1", "dev-1", generation)


@pytest.mark.asyncio
async def test_a_connect_declaring_an_incompatible_protocol_range_closes_1008():
    frame = json.dumps(
        {
            "type": "req",
            "id": "c-10",
            "method": "connect",
            "params": {
                "minProtocol": 5,
                "maxProtocol": 6,  # does not straddle node_broker.PROTOCOL_VERSION (4)
                "role": "node",
                "client": {"id": "node-host", "mode": "node", "platform": "macos"},
                "scopes": [],
                "commands": ["system.run"],
                "auth": {},
                "device": {"id": "dev-1"},
            },
        }
    )
    code = await gateway_relay._serve_node_protocol(
        _FakeSocket([frame], []), _identity(), {"runtime_host": "agentcore"}
    )
    assert code == 1008
    assert gateway_relay.node_sessions == {}


@pytest.mark.asyncio
async def test_1008_rejections_emit_the_relay_metric():
    with patch("routers.gateway_relay.put_metric") as metric:
        await gateway_relay._serve_node_protocol(_FakeSocket([], []), _identity(), {"runtime_host": "agentcore"})
    reasons = [
        call.kwargs["dimensions"]["reason"]
        for call in metric.call_args_list
        if call.args and call.args[0] == "relay.node_protocol"
    ]
    assert "challenge_timeout" in reasons
