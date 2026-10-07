"""The relay's OUTER handler: who is admitted, and what closes the socket.

The node protocol itself -- the challenge, the signed connect, framing, the invoke legs -- is
covered in ``test_gateway_relay_agentcore.py``. What is left here is the door: auth, the
kind fence, the workspace check, presence, and the close-code decision.

**There is no classic pipe any more.** The relay used to dial the owner's container over a second
WebSocket and pipe frames verbatim between the two, for both kinds; an AgentCore runtime serves no
WebSocket, so a node terminates in ``_serve_node_protocol`` and an operator -- which only ever
existed to be piped -- is refused outright. Every test in this file that drove that pipe (the
wake-then-1013, the upstream close-code propagation, the leg-exception path, the NOT_PAIRED
self-heal, the presence-refresh revoke through an operator) went with the mechanism.
"""

import asyncio
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

_PROVISIONED = {"owner_id": "org_1", "agentcore_runtime_arn": "arn:aws:x:1:runtime/r1"}


def _app():
    app = FastAPI()
    app.include_router(gateway_relay.router, prefix="/api/v1/gateway")
    return app


def _door(identity, *, container=_PROVISIONED, row_error=None):
    """Patch the door: auth, the workspace row, presence, and the node protocol itself.

    ``_serve_node_protocol`` is stubbed to a plain 1000 close -- this file is about whether the
    handler gets there, not what happens inside.
    """
    svc = MagicMock()
    svc.put_relay_presence = AsyncMock()
    svc.refresh_relay_presence = AsyncMock(return_value=True)
    svc.delete_connection = MagicMock()
    row = AsyncMock(side_effect=row_error) if row_error else AsyncMock(return_value=container)
    return (
        patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=identity),
        patch("routers.gateway_relay.container_repo.get_by_owner_id", row),
        patch("routers.gateway_relay.ConnectionService", return_value=svc),
        patch("routers.gateway_relay._serve_node_protocol", new_callable=AsyncMock, return_value=1000),
    ), svc


def _connect(url="/api/v1/gateway/ws?token=x&clientId=openclaw-macos"):
    with TestClient(_app()).websocket_connect(url):
        pass


def test_bad_token_closes_1008():
    with patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=None):
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect()
    assert exc.value.code == 1008


def test_an_operator_is_refused_rather_than_piped_to_nothing():
    """An operator only ever existed to be piped to the container's own WebSocket. An AgentCore
    runtime serves none, so accepting one would hang the client until its own timeout instead of
    telling it to use the HTTP facade."""
    (auth, row, conn, serve), _ = _door(OPERATOR)
    with auth, row, conn, serve as protocol:
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator")
    assert exc.value.code == 1008
    protocol.assert_not_awaited()


def test_an_operator_is_refused_before_the_workspace_is_even_read():
    """The kind fence is ahead of the row read, so a misconfigured operator that reconnects in a
    loop costs no DynamoDB reads."""
    (auth, row, conn, serve), _ = _door(OPERATOR)
    with auth, row as row_mock, conn, serve:
        with pytest.raises(WebSocketDisconnect):
            _connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator")
    row_mock.assert_not_awaited()


def test_an_unprovisioned_workspace_closes_1013_and_never_starts_anything():
    """RULING D14: a node's own reconnect -- a laptop coming back from sleep -- never started a
    workspace and must not now. There is nothing to wake either, since AgentCore starts the
    microVM on the invoke, so the connection is simply refused."""
    (auth, row, conn, serve), _ = _door(NODE, container=None)
    with auth, row, conn, serve as protocol:
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect()
    assert exc.value.code == 1013
    protocol.assert_not_awaited()


def test_a_workspace_read_failure_closes_1011():
    (auth, row, conn, serve), _ = _door(NODE, row_error=RuntimeError("ddb down"))
    with auth, row, conn, serve as protocol:
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect()
    assert exc.value.code == 1011
    protocol.assert_not_awaited()


def test_presence_write_failure_closes_1011_and_never_serves_the_protocol():
    """FAIL CLOSED, not best-effort: a connection with no presence row is invisible to revoke --
    nothing to delete, nothing to notice gone. The client's own reconnect loop retries."""
    (auth, row, conn, serve), svc = _door(NODE)
    svc.put_relay_presence = AsyncMock(side_effect=RuntimeError("ddb down"))
    with auth, row, conn, serve as protocol:
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect()
    assert exc.value.code == 1011
    protocol.assert_not_awaited()


def test_a_served_node_gets_its_presence_row_written_and_deleted():
    (auth, row, conn, serve), svc = _door(NODE)
    with auth, row, conn, serve:
        # The stub never accepts the socket, so the client sees the handler's own 1000 close.
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect()
    assert exc.value.code == 1000
    svc.put_relay_presence.assert_awaited_once()
    assert svc.put_relay_presence.await_args.kwargs["identity"] is NODE
    connection_id = svc.put_relay_presence.await_args.kwargs["connection_id"]
    svc.delete_connection.assert_called_once_with(connection_id)


def test_the_protocol_receives_the_workspace_row_the_handler_read():
    """It is read once at the door and threaded in -- a second read inside the protocol could
    answer differently mid-connection."""
    (auth, row, conn, serve), _ = _door(NODE)
    with auth, row, conn, serve as protocol:
        with pytest.raises(WebSocketDisconnect):
            _connect()
    assert protocol.await_args.args[2] == _PROVISIONED


def test_trailing_slash_route_is_accepted():
    (auth, row, conn, serve), _ = _door(NODE, container=None)
    with auth, row, conn, serve:
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect("/api/v1/gateway/ws/?token=x&clientId=openclaw-macos")
    assert exc.value.code == 1013


# --- the close-code decision, as a unit ------------------------------------------------------


def test_revoke_wins_the_close_code_over_a_same_batch_leg_failure():
    """When the presence refresher's revoke and a leg's exception land in the SAME
    asyncio.wait(FIRST_COMPLETED) batch, 1008 (revoked) must win over 1011 (leg failure) --
    presence loss is the stronger, more specific signal. Two trivial tasks that resolve without
    ever awaiting anything real complete before asyncio.wait is even entered (one
    asyncio.sleep(0) lets them both run), so they land in the SAME ``done`` set
    deterministically -- no timing race in the test itself."""

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
