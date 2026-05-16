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

    async def push(_):
        pass

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
