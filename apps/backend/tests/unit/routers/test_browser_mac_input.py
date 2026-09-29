"""POST /browser/mac/input: a viewer's input on an agent's Mac tab, routed to the task holding
the Mac's socket, and the peer route that receives the forward."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.services import mac_browser, mac_live, task_address
from core.services.connection_service import ConnectionService
from core.services.desktop_execution import DesktopError
from core.services.messaging import ApiProblem
from core.services.node_broker import CommandNotAllowed, NodeCapacity

OWNER = "org_test_456"  # the org member fixture's workspace
USER = "user_test_789"  # the org member fixture's user
ROOM = "22222222-2222-4222-8222-222222222222"
HERE = "http://10.0.0.1:8000"
PEER = "http://10.0.0.2:8000"
CLICK = {"type": "down", "x": 10, "y": 20, "button": "left", "clicks": 1}
BODY = {"runId": "run:1", "deviceId": "mac_1", "events": [CLICK]}


@pytest.fixture
def routing(monkeypatch):
    addresses = _Addresses()

    async def desktop_task_address(self, owner, device):
        return addresses.get((owner, device))

    monkeypatch.setattr(ConnectionService, "desktop_task_address", desktop_task_address)
    monkeypatch.setattr(task_address, "own_address", lambda: HERE)
    monkeypatch.setattr(mac_live, "_live", {})
    members = {(OWNER, USER, ROOM)}  # (workspace, human, room): who is in which run's room

    async def require_room_member(owner, user, room):
        if (owner, user, room) not in members:
            raise ApiProblem("ROOM_FORBIDDEN", "You no longer have access to this room.")
        return {}, {}

    monkeypatch.setattr(mac_live, "require_room_member", require_room_member)
    addresses.members = members
    return addresses


class _Addresses(dict):
    members: set


def _live(owner="org_test_456", run="run:1", error=None, room=ROOM):
    session = MagicMock()
    if error is not None:
        session.cast_input.side_effect = error
    mac_live._live[(owner, run)] = (session, object(), room)
    return session


@pytest.mark.asyncio
async def test_a_member_of_another_workspace_reaches_nothing(async_client_org_member, routing):
    routing[("org_other", "mac_1")] = HERE
    other = _live(owner="org_other")
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "mac_view_not_live"
    # Even with the Mac on this task, the run registered under another owner is not found.
    routing[(OWNER, "mac_1")] = HERE
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == 409 and response.json()["detail"] == {
        "code": "mac_view_not_live",
        "message": "That page isn't live on the Mac any more.",
    }
    other.cast_input.assert_not_called()


@pytest.mark.asyncio
async def test_local_delivery_calls_mac_live_input(async_client_org_member, routing):
    routing[(OWNER, "mac_1")] = HERE
    session = _live()
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    channel = mac_live._live[(OWNER, "run:1")][1]
    session.cast_input.assert_called_once_with(channel, [CLICK])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (CommandNotAllowed(), 409, "desktop_update_required"),
        (NodeCapacity(), 429, "mac_browser_busy"),
    ],
)
async def test_local_errors_map(async_client_org_member, routing, error, status, code):
    routing[(OWNER, "mac_1")] = HERE
    _live(error=error)
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code, "message": mac_browser.MESSAGES[code]}


@pytest.mark.asyncio
async def test_a_peer_address_forwards_with_the_secret(async_client_org_member, routing, monkeypatch):
    routing[(OWNER, "mac_1")] = PEER
    forward = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mac_browser, "_forward_mac", forward)
    events = [{"type": "key", "key": "Enter"}, {"type": "text", "text": "hi"}]
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json={**BODY, "events": events})
    assert response.status_code == 200
    forward.assert_awaited_once_with(
        PEER,
        {
            "ownerId": OWNER,
            "userId": USER,
            "runId": "run:1",
            "events": [{"type": "key", "key": "Enter", "modifiers": 0}, events[1]],
        },
        "/api/v1/desktop-execution/internal/mac-input",
        timeout=5,
    )


@pytest.mark.asyncio
async def test_forward_sends_the_internal_secret(monkeypatch):
    """_forward_mac carries x-lightbulb-ws-internal (the peer checks it)."""
    import httpx

    seen = {}

    async def handler(request):
        seen["secret"] = request.headers.get("x-lightbulb-ws-internal")
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await mac_browser._forward_mac(PEER, {"ownerId": OWNER}, "/api/v1/desktop-execution/internal/mac-input", 5)
    assert seen["secret"] == "s3cret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer", "status", "code"),
    [
        (DesktopError("mac_view_not_live", 409, "x"), 409, "mac_view_not_live"),
        (DesktopError("desktop_update_required", 409, "u"), 409, "desktop_update_required"),
        (DesktopError("room_forbidden", 403, "r"), 403, "room_forbidden"),
        (DesktopError("desktop_peer_unavailable", 503, "down"), 502, "desktop_peer_unavailable"),
    ],
)
async def test_peer_answers_map(async_client_org_member, routing, monkeypatch, peer, status, code):
    routing[(OWNER, "mac_1")] = PEER
    monkeypatch.setattr(mac_browser, "_forward_mac", AsyncMock(side_effect=peer))
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == status and response.json()["detail"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [{"type": "hover", "x": 1, "y": 1}],  # not a type
        [{"type": "down", "x": -1, "y": 1}],
        [{"type": "down", "x": 1, "y": 1, "clicks": 4}],
        [{"type": "down", "x": 1, "y": 1, "button": "back"}],
        [{"type": "wheel", "x": 1, "y": 1, "deltaY": 5001}],
        [{"type": "key", "key": "Enter", "modifiers": 16}],
        [{"type": "key", "key": ""}],
        [{"type": "paste", "text": "x" * 1001}],
        [{"type": "text", "text": "a", "extra": 1}],  # extra keys are refused
        [],
        [CLICK] * 33,
    ],
)
async def test_bad_events_422(async_client_org_member, routing, events):
    session = _live()
    routing[(OWNER, "mac_1")] = HERE
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json={**BODY, "events": events})
    assert response.status_code == 422
    session.cast_input.assert_not_called()


@pytest.mark.asyncio
async def test_bad_device_id_422(async_client_org_member, routing):
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json={**BODY, "deviceId": "../x"})
    assert response.status_code == 422


# ---- the peer route ---------------------------------------------------------------------------


@pytest.fixture
def peer(monkeypatch, routing):
    from routers import desktop_execution as desktop_router

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    app = FastAPI()
    app.include_router(desktop_router.router, prefix="/api/v1/desktop-execution")
    with TestClient(app) as client:
        yield client


def _peer_post(client, body, secret="s3cret"):
    return client.post(
        "/api/v1/desktop-execution/internal/mac-input", json=body, headers={"x-lightbulb-ws-internal": secret}
    )


PEER_BODY = {"ownerId": OWNER, "userId": USER, "runId": "run:1", "events": [CLICK]}


def test_peer_route_refuses_a_wrong_secret(peer):
    session = _live()
    assert _peer_post(peer, PEER_BODY, secret="nope").status_code == 403
    assert peer.post("/api/v1/desktop-execution/internal/mac-input", json=PEER_BODY).status_code == 403
    session.cast_input.assert_not_called()


def test_peer_route_delivers(peer):
    session = _live()
    assert _peer_post(peer, PEER_BODY).status_code == 200
    session.cast_input.assert_called_once()
    assert session.cast_input.call_args.args[1] == [CLICK]


def test_peer_route_409_when_not_live(peer):
    response = _peer_post(peer, PEER_BODY)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "mac_view_not_live"


@pytest.mark.parametrize(
    "body",
    [
        {**PEER_BODY, "extra": 1},
        {"ownerId": OWNER, "userId": USER, "runId": "run:1"},
        {"ownerId": OWNER, "runId": "run:1", "events": [CLICK]},  # no caller to check
        {**PEER_BODY, "userId": "../x"},
        {**PEER_BODY, "ownerId": "../x"},
        {**PEER_BODY, "events": [{"type": "text", "text": "x" * 1001}]},
        {**PEER_BODY, "events": [CLICK] * 33},
    ],
)
def test_peer_route_validates_the_body(peer, body):
    session = _live()
    assert _peer_post(peer, body).status_code == 400
    session.cast_input.assert_not_called()


# ---- the address lookup -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        # far future: the params are built at collection, and the full suite runs past an hour
        ({"taskAddress": {"S": PEER}, "ttl": {"N": str(int(time.time()) + 10**7)}}, PEER),
        ({"taskAddress": {"S": PEER}, "ttl": {"N": str(int(time.time()) - 1)}}, None),  # expired
        (None, None),
    ],
)
async def test_desktop_task_address(item, expected):
    service = ConnectionService()
    service._client = MagicMock()
    service._client.get_item.return_value = {"Item": item} if item else {}
    assert await service.desktop_task_address(OWNER, "mac_1") == expected
    kwargs = service._client.get_item.call_args.kwargs
    assert kwargs["Key"] == {"connectionId": {"S": f"desktop-ready#{OWNER}#mac_1"}}
    assert kwargs["ConsistentRead"] is True


def test_peer_route_takes_the_largest_valid_batch(peer):
    """32 x 1,000 astral characters, \\u-escaped: far past _body's 16 KB default."""
    import json

    session = _live()
    events = [{"type": "paste", "text": "\U0001f600" * 1000}] * 32
    content = json.dumps({**PEER_BODY, "events": events})  # ensure_ascii: 12 bytes per character
    assert len(content) > 380_000
    response = peer.post(
        "/api/v1/desktop-execution/internal/mac-input",
        content=content,
        headers={"x-lightbulb-ws-internal": "s3cret", "content-type": "application/json"},
    )
    assert response.status_code == 200
    assert session.cast_input.call_args.args[1][0]["text"] == "\U0001f600" * 1000


def test_peer_route_answers_an_oversized_body_413_not_500(peer):
    from routers.desktop_execution import MAC_INPUT_MAX_BYTES

    body = {**PEER_BODY, "events": [{"type": "paste", "text": "x" * (MAC_INPUT_MAX_BYTES + 1)}]}
    response = _peer_post(peer, body)
    assert response.status_code == 413 and response.json()["detail"] == {"code": "request_too_large"}


# ---- room membership (who can see the frames is who can drive them) ------------------------


@pytest.mark.asyncio
async def test_a_workspace_member_outside_the_runs_room_is_refused(async_client_org_member, routing):
    routing[(OWNER, "mac_1")] = HERE
    session = _live(room="33333333-3333-4333-8333-333333333333")  # a room the caller is not in
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "room_forbidden"
    session.cast_input.assert_not_called()


@pytest.mark.asyncio
async def test_removal_from_the_room_ends_input(async_client_org_member, routing):
    routing[(OWNER, "mac_1")] = HERE
    session = _live()
    assert (await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)).status_code == 200
    routing.members.clear()  # removed from the room while the run is still live
    response = await async_client_org_member.post("/api/v1/browser/mac/input", json=BODY)
    assert response.status_code == 403
    session.cast_input.assert_called_once()


def test_peer_route_checks_the_forwarded_caller_in_the_runs_room(peer):
    session = _live()
    response = _peer_post(peer, {**PEER_BODY, "userId": "user_outsider"})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "room_forbidden"
    session.cast_input.assert_not_called()
    assert _peer_post(peer, PEER_BODY).status_code == 200  # the room member
    session.cast_input.assert_called_once()
