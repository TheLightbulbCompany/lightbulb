"""Relay contract with a fake upstream: auth, wake, presence, verbatim piping, the two hooks."""

import asyncio
import json
import logging
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
        self.close_code = None

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
    # The delete_connection assertion below is on finally-block behavior, so
    # (same as test_node_ready_and_closed_hooks_fire_for_a_node) the test must
    # not exit `with ws:` until the SERVER has closed on its own -- a
    # client-initiated disconnect can race the relay's own teardown and
    # return before its finally block (and therefore delete_connection) has
    # run.
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with pytest.raises(WebSocketDisconnect):
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                ws.send_text('{"type":"req","id":"1","method":"connect","params":{"role":"node"}}')
                upstream.feed('{"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}')
                assert json.loads(ws.receive_text())["payload"]["type"] == "hello-ok"
                upstream.feed(b"\x00\x01")
                assert ws.receive_bytes() == b"\x00\x01"
                upstream.feed(None)
                ws.receive_text()  # blocks until the relay's own close (1000) propagates
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


# --- Task 3 review fix round ---


def test_revoked_presence_closes_the_socket_1008(monkeypatch):
    monkeypatch.setattr(gateway_relay, "_REFRESH_SECONDS", 0)
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    svc.refresh_relay_presence = AsyncMock(return_value=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                ws.receive_text()
    assert exc.value.code == 1008


def test_operator_not_paired_routes_through_the_same_self_heal_gate_as_a_node():
    """The deviceId in a NOT_PAIRED frame is untrusted (the operator's own
    identity carries no device_id) — the operator goes through the EXACT
    same backend-paired gate as a node's, via heal_desktop_device_not_paired
    (the "must be backend-paired before any approve RPC runs" gate is that
    function's own concern; see tests/unit/services/test_desktop_pair_live.py)."""
    upstream = FakeUpstream()
    patches, _ = _patches(OPERATOR, upstream)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch(
            "routers.gateway_relay.heal_desktop_device_not_paired", new_callable=AsyncMock, return_value=False
        ) as heal,
    ):
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator") as ws:
            upstream.feed(
                '{"type":"res","id":"1","ok":false,"error":{"code":"NOT_PAIRED","details":{"requestId":"req-1","deviceId":"'
                + "b" * 64
                + '"}}}'
            )
            ws.receive_text()
            upstream.feed(None)
    heal.assert_awaited_once_with(owner_id="org_1", device_id="b" * 64, request_id="req-1")


def test_operator_not_paired_approves_once_even_if_the_frame_repeats():
    upstream = FakeUpstream()
    patches, _ = _patches(OPERATOR, upstream)
    frame = (
        '{"type":"res","id":"1","ok":false,"error":{"code":"NOT_PAIRED","details":{"requestId":"req-1","deviceId":"'
        + "b" * 64
        + '"}}}'
    )
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
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator") as ws:
            upstream.feed(frame)
            ws.receive_text()
            upstream.feed(frame)
            ws.receive_text()
            upstream.feed(None)
    heal.assert_awaited_once_with(owner_id="org_1", device_id="b" * 64, request_id="req-1")


def test_node_ready_and_closed_hooks_fire_for_a_node():
    # Both assertions are on finally-block behavior (on_node_closed only runs
    # after the presence delete), so the test must not exit `with ws:` until
    # the SERVER has closed on its own -- a client-initiated disconnect (just
    # falling out of the block) can race the relay's own teardown and return
    # before its finally block has run.
    upstream = FakeUpstream()
    patches, _ = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as on_ready, patches[5] as on_closed, patches[6]:
        with pytest.raises(WebSocketDisconnect):
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                upstream.feed('{"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}')
                ws.receive_text()
                upstream.feed(None)
                ws.receive_text()  # blocks until the relay's own close (1000) propagates
    on_ready.assert_awaited_once_with("org_1", "user_1", "a" * 64)
    on_closed.assert_awaited_once_with("org_1", "user_1", stale_row_undeleted=False)


def test_trailing_slash_route_is_accepted():
    patches, _ = _patches(OPERATOR, FakeUpstream(), container=(None, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as wake:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws/?token=x&clientId=paperclip-operator"):
                pass
    assert exc.value.code == 1013
    wake.assert_awaited_once_with("org_1", door="relay")


def test_upstream_close_code_propagates_to_the_client():
    upstream = FakeUpstream()
    patches, _ = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                upstream.close_code = 4401
                upstream.feed(None)
                ws.receive_text()  # blocks until the relay's own close (now 4401) propagates
    assert exc.value.code == 4401


def test_leg_exception_closes_1011_and_logs_it(caplog):
    upstream = FakeUpstream()
    upstream.send = AsyncMock(side_effect=RuntimeError("boom"))
    patches, _ = _patches(NODE, upstream)
    with caplog.at_level(logging.WARNING, logger="routers.gateway_relay"):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with pytest.raises(WebSocketDisconnect) as exc:
                with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                    ws.send_text('{"type":"req","id":"1","method":"connect","params":{}}')
                    ws.receive_text()
    assert exc.value.code == 1011
    assert any("relay leg failed" in r.message for r in caplog.records)


def test_stopped_container_node_connect_never_wakes():
    patches, _ = _patches(NODE, FakeUpstream(), container=(None, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as wake:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos"):
                pass
    assert exc.value.code == 1013
    wake.assert_not_awaited()


# --- Whole-branch review fix round ---


def test_revoke_wins_the_close_code_over_a_same_batch_leg_failure():
    """When the presence refresher's revoke and a leg's exception land in the
    SAME asyncio.wait(FIRST_COMPLETED) batch, 1008 (revoked) must win over
    1011 (leg failure) -- presence loss is the stronger, more specific
    signal. Two trivial tasks that resolve without ever awaiting anything
    real complete before asyncio.wait is even entered (one asyncio.sleep(0)
    lets them both run), so they land in the SAME `done` set deterministically
    -- no timing race in the test itself."""

    async def revoked():
        return None

    async def failed():
        raise RuntimeError("boom")

    async def run():
        refresher = asyncio.create_task(revoked())
        leg = asyncio.create_task(failed())
        await asyncio.sleep(0)  # let both trivial tasks run to completion first
        done, _ = await asyncio.wait({refresher, leg}, return_when=asyncio.FIRST_COMPLETED)
        assert refresher in done and leg in done  # the race this test targets
        code = gateway_relay._pick_close_code(done, refresher)
        leg.exception()  # consume, or asyncio logs "exception never retrieved"
        return code

    assert asyncio.run(run()) == 1008


def test_pick_close_code_returns_1011_for_a_leg_failure_alone():
    async def failed():
        raise RuntimeError("boom")

    async def run():
        leg = asyncio.create_task(failed())
        await asyncio.sleep(0)
        done, _ = await asyncio.wait({leg}, return_when=asyncio.FIRST_COMPLETED)
        return gateway_relay._pick_close_code(done, None)

    assert asyncio.run(run()) == 1011


def test_presence_write_failure_closes_1011_and_never_dials_upstream():
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    svc.put_relay_presence = AsyncMock(side_effect=RuntimeError("ddb down"))
    with patches[0], patches[1], patches[2], patches[3] as ws_connect_mock, patches[4], patches[5], patches[6]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos"):
                pass
    assert exc.value.code == 1011
    ws_connect_mock.assert_not_called()  # fail closed -- never opens the upstream leg


def test_delete_connection_failure_still_floors_the_node_count():
    """A finally-block delete_connection failure must not silently skip the
    last-node re-patch decision -- on_node_closed is told the row may still
    be sitting in DDB via stale_row_undeleted=True (relay_hooks own test
    covers the count-flooring math; this proves the flag is threaded)."""
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    svc.delete_connection = MagicMock(side_effect=RuntimeError("ddb down"))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as on_closed, patches[6]:
        with pytest.raises(WebSocketDisconnect):
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
                upstream.feed('{"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}')
                ws.receive_text()
                upstream.feed(None)
                ws.receive_text()  # blocks until the relay's own close propagates
    on_closed.assert_awaited_once_with("org_1", "user_1", stale_row_undeleted=True)
