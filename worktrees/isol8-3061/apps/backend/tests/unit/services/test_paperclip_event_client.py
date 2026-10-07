"""Unit tests for PaperclipEventClient.

Uses websockets.serve to spin up a fake Paperclip endpoint inside the
test process so we exercise the full WS roundtrip (handshake, frame,
parse, callback dispatch) without mocking websockets internals.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import patch

import pytest
import websockets

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


async def _make_fake_server(handler):
    """Start a websockets server bound to a free localhost port."""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_event_client_receives_and_dispatches_event():
    """Server sends one event; client invokes the on_event callback."""
    from core.services.paperclip_event_client import PaperclipEventClient

    received: list[dict] = []

    async def handler(ws):
        # Verify cookie header was sent. websockets 13+ exposes the
        # client request via ws.request.headers (a multidict).
        headers = ws.request.headers
        cookie = headers.get("cookie") or headers.get("Cookie")
        assert cookie == "test-session=abc"
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "companyId": "co_x",
                    "type": "activity.logged",
                    "createdAt": "2026-05-04T01:00:00Z",
                    "payload": {"actor": "u1"},
                }
            )
        )
        await ws.wait_closed()

    server, base_url = await _make_fake_server(handler)

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    client = PaperclipEventClient(
        url=base_url,
        cookie="test-session=abc",
        on_event=on_event,
    )
    await client.start()
    # Wait briefly for the event to flow.
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)
    await client.close()
    server.close()
    await server.wait_closed()

    assert len(received) == 1
    assert received[0]["type"] == "activity.logged"
    assert received[0]["payload"] == {"actor": "u1"}


@pytest.mark.asyncio
async def test_event_client_emits_synthetic_stream_resumed_on_reconnect():
    """After server-side disconnect + client reconnect, on_event sees a
    synthetic ``{type: 'stream.resumed'}`` event so the broker can flush
    SWR caches. Spec § Reconnect semantics."""
    from core.services.paperclip_event_client import PaperclipEventClient

    received: list[dict] = []
    connect_count = 0

    async def handler(ws):
        nonlocal connect_count
        connect_count += 1
        if connect_count == 1:
            # First connection: send one real event then drop the socket.
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "companyId": "co_x",
                        "type": "activity.logged",
                        "createdAt": "2026-05-04T01:00:00Z",
                        "payload": {},
                    }
                )
            )
            await ws.close()
        else:
            # Hold the second connection open until close().
            await ws.wait_closed()

    server, base_url = await _make_fake_server(handler)

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    client = PaperclipEventClient(
        url=base_url,
        cookie="c=1",
        on_event=on_event,
        reconnect_initial_delay=0.05,  # keep test fast
    )
    await client.start()
    # Wait for: real event from connect 1 + synthetic resumed from connect 2.
    for _ in range(100):
        if any(e.get("type") == "stream.resumed" for e in received):
            break
        await asyncio.sleep(0.05)
    await client.close()
    server.close()
    await server.wait_closed()

    types = [e["type"] for e in received]
    assert "activity.logged" in types
    assert "stream.resumed" in types
    # stream.resumed must come AFTER the real event (post-reconnect).
    assert types.index("stream.resumed") > types.index("activity.logged")


@pytest.mark.asyncio
async def test_event_client_close_stops_reconnect_loop():
    """After close(), no further reconnect attempts run even if the
    server keeps dropping connections."""
    from core.services.paperclip_event_client import PaperclipEventClient

    connect_count = 0

    async def handler(ws):
        nonlocal connect_count
        connect_count += 1
        await ws.close()

    server, base_url = await _make_fake_server(handler)

    async def on_event(event: dict[str, Any]) -> None:
        pass

    client = PaperclipEventClient(
        url=base_url,
        cookie="c=1",
        on_event=on_event,
        reconnect_initial_delay=0.05,
    )
    await client.start()
    await asyncio.sleep(0.2)  # let it reconnect a few times
    await client.close()
    count_at_close = connect_count
    await asyncio.sleep(0.3)  # if reconnect loop is still alive, count grows
    server.close()
    await server.wait_closed()

    # Allow at most 1 in-flight attempt after close (race tolerance).
    assert connect_count <= count_at_close + 1


@pytest.mark.asyncio
async def test_event_client_ignores_malformed_messages():
    """Non-JSON messages are logged + dropped, not crash the loop."""
    from core.services.paperclip_event_client import PaperclipEventClient

    received: list[dict] = []

    async def handler(ws):
        await ws.send("this is not json")
        await ws.send(
            json.dumps(
                {
                    "id": 2,
                    "companyId": "co_x",
                    "type": "agent.status",
                    "createdAt": "2026-05-04T01:00:00Z",
                    "payload": {"agentId": "a1"},
                }
            )
        )
        await ws.wait_closed()

    server, base_url = await _make_fake_server(handler)

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    client = PaperclipEventClient(
        url=base_url,
        cookie="c=1",
        on_event=on_event,
    )
    await client.start()
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)
    await client.close()
    server.close()
    await server.wait_closed()

    assert len(received) == 1
    assert received[0]["type"] == "agent.status"


@pytest.mark.asyncio
async def test_event_client_emits_connect_metric_on_initial_connect():
    """Happy path: first successful connect emits teams.client.connect with
    outcome=ok and kind=initial."""
    from core.services.paperclip_event_client import PaperclipEventClient

    received: list[dict] = []

    async def handler(ws):
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "companyId": "co_x",
                    "type": "activity.logged",
                    "createdAt": "2026-05-04T01:00:00Z",
                    "payload": {},
                }
            )
        )
        await ws.wait_closed()

    server, base_url = await _make_fake_server(handler)

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    with patch("core.services.paperclip_event_client.put_metric") as put_metric_mock:
        client = PaperclipEventClient(
            url=base_url,
            cookie="c=1",
            on_event=on_event,
        )
        await client.start()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        await client.close()
        server.close()
        await server.wait_closed()

    # At least one call to teams.client.connect with outcome=ok, kind=initial.
    matching = [
        c
        for c in put_metric_mock.call_args_list
        if c.args
        and c.args[0] == "teams.client.connect"
        and c.kwargs.get("dimensions", {}).get("outcome") == "ok"
        and c.kwargs.get("dimensions", {}).get("kind") == "initial"
    ]
    assert matching, (
        f"expected teams.client.connect ok/initial; got: {[(c.args, c.kwargs) for c in put_metric_mock.call_args_list]}"
    )


@pytest.mark.asyncio
async def test_event_client_passes_user_company_through_for_logging(caplog):
    """The constructor accepts user_id + company_id and stores them as
    instance state for log-correlation. No behavior change — purely a
    logging-context concern."""
    from core.services.paperclip_event_client import PaperclipEventClient

    async def on_event(event):
        pass

    client = PaperclipEventClient(
        url="ws://localhost:1",
        cookie="x",
        on_event=on_event,
        user_id="user_abc",
        company_id="co_xyz",
    )
    assert client._user_id == "user_abc"
    assert client._company_id == "co_xyz"
