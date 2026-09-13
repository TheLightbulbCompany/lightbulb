"""Relay contract with a fake upstream: auth, wake, presence, verbatim piping, the two hooks."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.services.machine_auth import MachineIdentity
from routers import gateway_relay

NODE = MachineIdentity(
    user_id="user_1",
    org_id="org_1",
    owner_id="org_1",
    kind="node",
    auth_kind="device",
    client_id="openclaw-macos",
    device_id="a" * 64,
)
OPERATOR = MachineIdentity(
    user_id="org_1",
    org_id="",
    owner_id="org_1",
    kind="operator",
    auth_kind="paperclip_service",
    client_id="paperclip-operator",
    device_id=None,
)


class FakeUpstream:
    """Minimal async-iterable websocket: `send` records, `feed` pushes frames to the relay."""

    def __init__(self):
        self.sent, self._queue = [], asyncio.Queue()

    async def send(self, frame):
        self.sent.append(frame)

    def feed(self, frame):
        self._queue.put_nowait(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._queue.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _app():
    app = FastAPI()
    app.include_router(gateway_relay.router, prefix="/api/v1/gateway")
    return app


def _patches(identity, upstream, container=({"gateway_token": "gw"}, "10.0.0.9")):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=container)
    svc = MagicMock()
    svc.put_relay_presence = AsyncMock()
    svc.refresh_relay_presence = AsyncMock(return_value=True)
    svc.delete_connection = MagicMock()
    return (
        patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=identity),
        patch("routers.gateway_relay.get_ecs_manager", return_value=manager),
        patch("routers.gateway_relay.ConnectionService", return_value=svc),
        patch("routers.gateway_relay.ws_connect", return_value=upstream),
        patch("routers.gateway_relay.relay_hooks.on_node_ready", new_callable=AsyncMock),
        patch("routers.gateway_relay.relay_hooks.on_node_closed", new_callable=AsyncMock),
        patch("routers.gateway_relay.request_wake_silent", new_callable=AsyncMock, return_value="started"),
    ), svc


def test_bad_token_closes_1008():
    with patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=None):
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=node-host"):
                pass
    assert exc.value.code == 1008


def test_stopped_container_wakes_and_closes_1013():
    patches, _ = _patches(OPERATOR, FakeUpstream(), container=(None, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as wake:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator"):
                pass
    assert exc.value.code == 1013
    wake.assert_awaited_once_with("org_1", door="relay")


def test_frames_are_piped_verbatim_and_presence_is_written_then_deleted():
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
            ws.send_text('{"type":"req","id":"1","method":"connect","params":{"role":"node"}}')
            upstream.feed('{"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}')
            assert json.loads(ws.receive_text())["payload"]["type"] == "hello-ok"
            upstream.feed(b"\x00\x01")
            assert ws.receive_bytes() == b"\x00\x01"
            upstream.feed(None)
    assert upstream.sent[0] == '{"type":"req","id":"1","method":"connect","params":{"role":"node"}}'
    row = svc.put_relay_presence.await_args.kwargs
    assert row["identity"] == NODE and row["connection_id"].startswith("relay#")
    svc.delete_connection.assert_called_once_with(row["connection_id"])


def test_operator_wake_gate_answers_the_client_and_drops_the_frame():
    upstream = FakeUpstream()
    patches, _ = _patches(OPERATOR, upstream)
    refusal = {
        "type": "res",
        "id": "9",
        "ok": False,
        "error": {
            "code": "TRIAL_EXPIRED",
            "message": "x",
            "details": {"reason": "wake_admission_gate", "code": "TRIAL_EXPIRED"},
        },
    }
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch("routers.gateway_relay.relay_hooks.admit_operator_wake", new_callable=AsyncMock, return_value=refusal),
    ):
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator") as ws:
            ws.send_text('{"type":"req","id":"9","method":"agent","params":{}}')
            assert json.loads(ws.receive_text()) == refusal
            upstream.feed(None)
    assert upstream.sent == []


def test_not_paired_from_the_container_triggers_the_self_heal():
    upstream = FakeUpstream()
    patches, _ = _patches(NODE, upstream)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch(
            "routers.gateway_relay.heal_desktop_device_not_paired", new_callable=AsyncMock, return_value=True
        ) as heal,
    ):
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
            upstream.feed(
                '{"type":"res","id":"1","ok":false,"error":{"code":"NOT_PAIRED","details":{"requestId":"req-1","deviceId":"'
                + "a" * 64
                + '"}}}'
            )
            assert json.loads(ws.receive_text())["error"]["code"] == "NOT_PAIRED"  # still forwarded
            upstream.feed(None)
    heal.assert_awaited_once_with(owner_id="org_1", device_id="a" * 64, request_id="req-1")
