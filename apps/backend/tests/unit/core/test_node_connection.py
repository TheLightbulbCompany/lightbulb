"""Tests for the post-migration NodeUpstreamConnection (pure relay).

After Task 14, backend no longer holds device private keys -- clients own
them in macOS Keychain. NodeUpstreamConnection is a pure protocol relay:
1. Open upstream WS to container with role:"node".
2. Receive container's connect.challenge.
3. Push the challenge DOWN to the client via the push callback.
4. Await the client's signed connect (delivered via submit_signed_connect).
5. Forward signed connect upstream.
6. Receive hello-ok upstream and return it (so handle_node_connect can
   forward it to the client).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_ws():
    """A mock WebSocket implementing send/recv/close (async)."""
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    return ws


async def test_connect_relays_challenge_and_returns_hello_ok(monkeypatch, mock_ws):
    """End-to-end: upstream challenge -> push to client -> client signs ->
    forward upstream -> hello-ok returned."""
    from core.gateway import node_connection

    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "n0nce"},
                }
            ),
            json.dumps(
                {
                    "type": "res",
                    "id": "req_signed",
                    "ok": True,
                    "payload": {"hello": "ok", "protocol": 4},
                }
            ),
        ]
    )

    async def fake_connect(url, **kwargs):
        return mock_ws

    monkeypatch.setattr(node_connection, "ws_connect", fake_connect)

    pushed_to_client: list[dict] = []

    async def push_callback(frame):
        pushed_to_client.append(frame)

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="10.0.0.1",
        node_connect_params={"client": {"id": "node-host"}, "role": "node"},
        gateway_token="gw_token_xyz",
    )
    conn.set_client_push_callback(push_callback)

    # Simulate the client signing and replying after the challenge has been
    # forwarded down. The test orchestrates the "client" out-of-band by
    # scheduling submit_signed_connect on the event loop.
    async def signer():
        # Yield until the push has happened (the connect() coroutine pushes
        # before awaiting the signed future).
        for _ in range(20):
            await asyncio.sleep(0.01)
            if pushed_to_client:
                break
        conn.submit_signed_connect(
            {
                "type": "req",
                "id": "req_signed",
                "method": "connect",
                "params": {
                    "device": {"id": "dev_hash_abc"},
                    "auth": {"token": "gw_token_xyz"},
                    "role": "node",
                    "client": {"id": "node-host"},
                },
            }
        )

    asyncio.create_task(signer())

    hello = await conn.connect()
    assert hello.get("ok") is True
    assert hello["payload"]["hello"] == "ok"
    assert conn.device_id == "dev_hash_abc"

    # The push callback should have received the upstream challenge.
    assert any(f.get("event") == "connect.challenge" for f in pushed_to_client), (
        f"expected challenge pushed; got {pushed_to_client}"
    )

    # The signed connect should have been forwarded upstream.
    sent_strs = [call.args[0] for call in mock_ws.send.call_args_list]
    assert any("req_signed" in s for s in sent_strs), f"signed connect not forwarded upstream; sends={sent_strs}"


async def test_connect_raises_on_upstream_rejection(monkeypatch, mock_ws):
    """If the container rejects the signed connect, connect() must raise."""
    from core.gateway import node_connection

    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "n"},
                }
            ),
            json.dumps(
                {
                    "type": "res",
                    "id": "x",
                    "ok": False,
                    "error": {"message": "bad signature"},
                }
            ),
        ]
    )

    async def fake_connect(url, **kwargs):
        return mock_ws

    monkeypatch.setattr(node_connection, "ws_connect", fake_connect)

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="10.0.0.1",
        node_connect_params={"client": {"id": "node-host"}, "role": "node"},
        gateway_token="t",
    )

    pushed = []

    async def push(frame):
        pushed.append(frame)

    conn.set_client_push_callback(push)

    async def signer():
        await asyncio.sleep(0.01)
        conn.submit_signed_connect(
            {
                "type": "req",
                "id": "x",
                "method": "connect",
                "params": {"device": {"id": "d"}},
            }
        )

    asyncio.create_task(signer())

    with pytest.raises(RuntimeError, match="bad signature|rejected|handshake"):
        await conn.connect()

    # The rejection res frame is forwarded downstream (keyed to the client's
    # own request id) before the raise — a direct gateway connection would
    # deliver it, and hiding it leaves the client's pending connect to time
    # out into a generic "gateway request timeout (connect)" instead of the
    # real reason (e.g. NOT_PAIRED).
    rejections = [f for f in pushed if f.get("type") == "res" and f.get("ok") is False]
    assert len(rejections) == 1
    assert rejections[0]["id"] == "x"
    assert rejections[0]["error"]["message"] == "bad signature"


async def test_device_id_and_rejection_error_populated_on_not_paired_rejection(monkeypatch, mock_ws):
    """Regression guard for the moved assignment: device_id must be populated
    from the client's own signed connect BEFORE a NOT_PAIRED rejection can
    raise, since a self-heal caller needs it to approve the pending pairing
    request. Also pins that the structured rejection error (code + details)
    is captured on the connection object rather than only living inside the
    raised RuntimeError's (500-char-truncated) message string."""
    from core.gateway import node_connection

    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "n"},
                }
            ),
            json.dumps(
                {
                    "type": "res",
                    "id": "x",
                    "ok": False,
                    "error": {
                        "code": "NOT_PAIRED",
                        "message": "device not paired",
                        "details": {"requestId": "req-42", "deviceId": "dev_rejected"},
                    },
                }
            ),
        ]
    )

    async def fake_connect(url, **kwargs):
        return mock_ws

    monkeypatch.setattr(node_connection, "ws_connect", fake_connect)

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="10.0.0.1",
        node_connect_params={},
        gateway_token="t",
    )
    conn.set_client_push_callback(AsyncMock())

    async def signer():
        await asyncio.sleep(0.01)
        conn.submit_signed_connect(
            {
                "type": "req",
                "id": "x",
                "method": "connect",
                "params": {"device": {"id": "dev_rejected"}},
            }
        )

    asyncio.create_task(signer())

    with pytest.raises(RuntimeError):
        await conn.connect()

    # device_id is set from the client's own signed connect, not derived from
    # the container's rejection payload -- and it survives the raise.
    assert conn.device_id == "dev_rejected"
    # The structured error is exposed for a caller to act on (e.g. self-heal
    # the pairing), independent of the truncated JSON string embedded in the
    # RuntimeError message.
    assert conn.rejection_error == {
        "code": "NOT_PAIRED",
        "message": "device not paired",
        "details": {"requestId": "req-42", "deviceId": "dev_rejected"},
    }


async def test_submit_signed_connect_idempotent_when_future_done():
    """Calling submit_signed_connect after the future resolves is a no-op."""
    from core.gateway import node_connection

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="x",
        node_connect_params={},
        gateway_token="t",
    )
    # Allocate the future on the running loop the way connect() does.
    conn._signed_connect_future = asyncio.get_event_loop().create_future()
    conn.submit_signed_connect({"a": 1})
    # Second call MUST NOT raise InvalidStateError on the already-resolved future.
    conn.submit_signed_connect({"a": 2})
    # Future stays resolved with the first value (no overwrite).
    assert conn._signed_connect_future.result() == {"a": 1}


async def test_submit_signed_connect_no_future_is_safe():
    """If no connect() is in flight, submit_signed_connect is a no-op."""
    from core.gateway import node_connection

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="x",
        node_connect_params={},
        gateway_token="t",
    )
    # No connect() ever awaited the future -- submit must not crash.
    conn.submit_signed_connect({"a": 1})


async def test_signed_connect_future_allocated_before_challenge_push(monkeypatch, mock_ws):
    """Race regression: a fast client that signs synchronously inside the
    push-callback must not have its signed-connect dropped as "no pending
    future". connect() must allocate ``_signed_connect_future`` BEFORE
    delivering the challenge to the client.

    Without the fix, the push callback runs submit_signed_connect before
    the future exists, the call is dropped, and connect() hangs the full
    _SIGNED_CONNECT_TIMEOUT.
    """
    from core.gateway import node_connection

    mock_ws.recv = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "n0nce"},
                }
            ),
            json.dumps(
                {
                    "type": "res",
                    "id": "req_signed",
                    "ok": True,
                    "payload": {"hello": "ok", "protocol": 4},
                }
            ),
        ]
    )

    async def fake_connect(url, **kwargs):
        return mock_ws

    monkeypatch.setattr(node_connection, "ws_connect", fake_connect)

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="10.0.0.1",
        node_connect_params={"client": {"id": "node-host"}, "role": "node"},
        gateway_token="t",
    )

    # The push callback simulates the synchronous "fast client" case: it
    # runs submit_signed_connect inline as soon as the challenge is pushed
    # downstream. If the future hasn't been allocated yet, the signed
    # frame is dropped and the test below would time out the loop.
    async def push_callback_signing_immediately(frame):
        if frame.get("event") == "connect.challenge":
            conn.submit_signed_connect(
                {
                    "type": "req",
                    "id": "req_signed",
                    "method": "connect",
                    "params": {"device": {"id": "dev_hash_fast"}},
                }
            )

    conn.set_client_push_callback(push_callback_signing_immediately)

    # Guard against the bug's symptom (10s hang). The fix should resolve
    # the future immediately, so a 2s budget is plenty of headroom for
    # the rest of the handshake.
    hello = await asyncio.wait_for(conn.connect(), timeout=2.0)
    assert hello.get("ok") is True
    assert conn.device_id == "dev_hash_fast"


async def test_reader_forwards_upstream_frames_to_client(monkeypatch, mock_ws):
    """After connect, start_reader forwards upstream frames to the push callback."""
    from core.gateway import node_connection

    # Make recv return one steady-state frame, then close.
    frames = [
        json.dumps({"type": "req", "id": "rpc-1", "method": "node.invoke.request"}),
    ]

    async def aiter():
        for f in frames:
            yield f

    mock_ws.__aiter__ = lambda self: aiter()

    pushed: list[dict] = []

    async def push(frame):
        pushed.append(frame)

    conn = node_connection.NodeUpstreamConnection(
        user_id="u",
        owner_id="o",
        container_ip="x",
        node_connect_params={},
        gateway_token="t",
    )
    conn.set_client_push_callback(push)
    conn._ws = mock_ws  # bypass connect() for this focused test

    await conn.start_reader()
    # Let the reader task drain.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pushed:
            break

    assert any(p.get("id") == "rpc-1" for p in pushed), f"upstream frame not pushed; got {pushed}"


def test_signed_connect_timeout_tolerates_observed_client_latency():
    """The signed-connect budget must exceed real-world client latency.

    Regression guard for the desktop node-host flap (2026-08-03). The budget
    was 10s on the assumption of a ~300ms round trip. Prod API Gateway logs
    showed a clean handshake where the Mac replied 13s after the challenge
    push, with backend integrationLatency of 111ms — the delay was entirely
    client-side, caused by macOS App Nap throttling the Tauri relay process
    (scheduling priority 4 backgrounded vs 46-54 foregrounded).

    At 10s that routine background state became a PERMANENT flap: the
    upstream is torn down on timeout, so the late-but-valid signature lands
    on the "no upstream" branch and is discarded, and the subprocess retries
    into the same wall forever. Observed range was 13-18s.

    Lowering this back under the observed range re-breaks desktop browser
    control for any user whose app is not in the foreground.
    """
    from core.gateway.node_connection import _SIGNED_CONNECT_TIMEOUT

    assert _SIGNED_CONNECT_TIMEOUT >= 25.0, (
        "signed-connect budget must stay above the 13-18s App-Nap-throttled "
        f"client latency observed in prod; got {_SIGNED_CONNECT_TIMEOUT}s"
    )
