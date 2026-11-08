"""The Mac CDP door: ticket -> authority -> the node socket's CDP channel, and every close code."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.config import settings
from core.services import mac_cdp
from core.services.desktop_execution import DesktopError
from core.services.node_broker import CdpClosed, NodeCapacity
from routers import gateway_relay
from routers import mac_cdp as door

ADDRESS = "http://10.0.3.7:8000"
TARGET = {
    "taskAddress": ADDRESS,
    "ownerId": "org_1",
    "memberId": "user_1",
    "deviceId": "mac1",
    "clientId": "openclaw-macos",
    "pairingGroupId": "pg1",
    "generation": "g1",
    "commands": ["browser.cdp.v1"],
}
ROOM = {"ownerId": "org_1", "memberId": "user_1", "runId": "r1", "agentId": "a1", "family": "room", "roomId": "rm1"}


class FakeChannel:
    def __init__(self):
        self.close_code = None
        self.queue = asyncio.Queue()  # created inside the app's loop

    def end(self, code):
        self.close_code = code
        self.queue.put_nowait(None)

    async def next(self):
        return await self.queue.get()


class FakeSession:
    """Plays the Mac: echoes each frame; "close:<CODE>" makes the Mac close; "drop" drops the socket."""

    can_cast = False  # no browser.cast.v1: mac_live.run returns at once

    def __init__(self):
        self.generation = "g1"
        self.open_error = self.send_error = None
        self.sent, self.closed, self.opened_with, self.notices = [], [], [], []

    async def cdp_open(self, profile_id, notice=None):
        self.opened_with.append(profile_id)
        self.notices.append(notice)
        if self.open_error:
            raise self.open_error
        return FakeChannel()

    def cdp_send(self, channel, data):
        if self.send_error:
            raise self.send_error
        self.sent.append(data)
        if data.startswith("close:"):
            channel.end(data.removeprefix("close:"))
        elif data == "drop":
            gateway_relay.node_sessions.pop(("org_1", "mac1"), None)
            channel.end("CLOSED")  # what fail_pending does
        else:
            channel.queue.put_nowait("echo:" + data)

    def cdp_close(self, channel, code="CLOSED"):
        self.closed.append(code)


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(mac_cdp.task_address, "own_address", lambda: ADDRESS)
    monkeypatch.setattr(mac_cdp, "check", AsyncMock(return_value=TARGET))
    monkeypatch.setattr(mac_cdp, "_meter", lambda outcome: outcomes.append(outcome))
    outcomes: list = []
    mac_cdp._redeemed.clear()
    session = FakeSession()
    session.outcomes = outcomes
    monkeypatch.setitem(gateway_relay.node_sessions, ("org_1", "mac1"), session)
    return session


def _client():
    app = FastAPI()
    app.include_router(door.router, prefix="/api/v1/internal/mac-cdp")
    return TestClient(app)


def _url():
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)
    return "/api/v1/internal/mac-cdp/" + ticket


def _closed_with(url, send=None):
    with _client().websocket_connect(url) as ws:
        if send is not None:
            ws.send_text(send)
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_text()
    return exc.value.code, exc.value.reason


def test_door_pipes_frames_both_ways(mac):
    with _client().websocket_connect(_url()) as ws:
        ws.send_text('{"id":1,"method":"Page.navigate"}')
        assert ws.receive_text() == 'echo:{"id":1,"method":"Page.navigate"}'
        ws.send_text("two")
        assert ws.receive_text() == "echo:two"
    assert mac.opened_with == ["p1"] and mac.notices == [None]  # a room run: the pairer asked
    assert mac.sent == ['{"id":1,"method":"Page.navigate"}', "two"]


def test_door_closes_4401_on_bad_ticket(mac):
    assert _closed_with("/api/v1/internal/mac-cdp/garbage")[0] == 4401
    url = _url()
    with _client().websocket_connect(url) as ws:
        ws.send_text("x")
        ws.receive_text()
    assert _closed_with(url)[0] == 4401  # replayed
    assert mac.opened_with == ["p1"] and "bad_ticket" in mac.outcomes


def test_door_closes_4409_when_socket_generation_gone(mac):
    mac.generation = "g2"
    assert _closed_with(_url())[0] == 4409
    assert mac.opened_with == []
    # A socket that drops mid-pipe ends the channel CLOSED, but that is a loss, not a normal close.
    mac.generation = "g1"
    assert _closed_with(_url(), send="drop")[0] == 4409


def test_door_closes_4502_with_mac_code(mac):
    mac.open_error = CdpClosed("BROWSER_OFF")
    assert _closed_with(_url()) == (4502, "BROWSER_OFF")
    mac.open_error = None
    assert _closed_with(_url(), send="close:PROFILE_CHANGED") == (4502, "PROFILE_CHANGED")
    assert _closed_with(_url(), send="close:CLOSED") == (1000, "")


def test_door_overflow_is_4502_not_1000(mac):
    assert _closed_with(_url(), send="close:OVERFLOW") == (4502, "OVERFLOW")
    assert mac.closed == ["CLOSED"]


def test_door_send_capacity_closes_4503(mac):
    mac.send_error = NodeCapacity()
    assert _closed_with(_url(), send="burst") == (4503, "CAPACITY")
    mac.send_error = CdpClosed("TOO_LARGE")
    assert _closed_with(_url(), send="huge") == (4502, "TOO_LARGE")
    mac.send_error, mac.open_error = None, NodeCapacity()
    assert _closed_with(_url()) == (4503, "CAPACITY")


def test_door_recheck_closes_4403_when_run_ends(mac, monkeypatch):
    monkeypatch.setattr(mac_cdp, "RECHECK_SECONDS", 0.05)
    mac_cdp.check.side_effect = [TARGET, DesktopError("run_not_active", 403)]
    assert _closed_with(_url())[0] == 4403
    assert mac.closed == ["CLOSED"] and mac.outcomes == ["forbidden"]


def test_door_recheck_retries_transient_errors(mac, monkeypatch):
    monkeypatch.setattr(mac_cdp, "RECHECK_SECONDS", 0.02)
    boom = RuntimeError("dynamodb")
    # Two transient failures, then a pass reset the count; three in a row close 1011.
    mac_cdp.check.side_effect = [TARGET, boom, boom, TARGET, boom, boom, boom]
    assert _closed_with(_url()) == (1011, "")
    assert mac_cdp.check.await_count == 7 and mac.outcomes == ["error"]


@pytest.mark.parametrize("code", ["desktop_generation_stale", "desktop_task_moved", "desktop_not_ready"])
def test_door_recheck_closes_4409_when_the_mac_slept(mac, monkeypatch, code):
    """Sleep/wake is not "access ended": the bridge re-acquires on 4409."""
    monkeypatch.setattr(mac_cdp, "RECHECK_SECONDS", 0.02)
    mac_cdp.check.side_effect = [TARGET, DesktopError(code, 409)]
    assert _closed_with(_url())[0] == 4409
    assert mac.outcomes == ["lost"]


@pytest.mark.parametrize("code", ["desktop_forbidden", "desktop_disabled", "run_forbidden", "run_not_active"])
def test_door_recheck_closes_4403_on_revocation(mac, monkeypatch, code):
    monkeypatch.setattr(mac_cdp, "RECHECK_SECONDS", 0.02)
    mac_cdp.check.side_effect = [TARGET, DesktopError(code, 403)]
    assert _closed_with(_url())[0] == 4403


def test_door_first_check_failure_mapping(mac):
    mac_cdp.check.side_effect = DesktopError("desktop_not_ready", 409)
    assert _closed_with(_url())[0] == 4409
    mac_cdp.check.side_effect = DesktopError("desktop_task_moved", 409)
    assert _closed_with(_url())[0] == 4409
    mac_cdp.check.side_effect = DesktopError("desktop_disabled", 403)
    assert _closed_with(_url())[0] == 4403
    assert mac.opened_with == []


def test_connection_is_capped(mac, monkeypatch):
    monkeypatch.setattr(mac_cdp, "AGENT_MAX_SECONDS", 0.1)
    assert _closed_with(_url()) == (1000, "MAX_AGE")
    assert mac.closed == ["CLOSED"]


def test_channel_close_failure_is_contained(mac):
    def explode(channel, code="CLOSED"):
        raise RuntimeError("queue gone")

    mac.cdp_close = explode
    assert _closed_with(_url(), send="close:CLOSED") == (1000, "")


def test_client_close_closes_the_channel(mac):
    with _client().websocket_connect(_url()) as ws:
        ws.send_text("x")
        ws.receive_text()
    assert mac.closed == ["CLOSED"] and mac.outcomes == ["ok"]


def test_ticket_never_logged(mac, caplog):
    caplog.set_level(logging.DEBUG)
    url = _url()
    with _client().websocket_connect(url) as ws:
        ws.send_text("frame-body")
        ws.receive_text()
    mac_cdp.check.side_effect = RuntimeError("boom")
    assert _closed_with(_url())[0] == 1011
    ticket = url.rsplit("/", 1)[1]
    assert "owner=org_1" in caplog.text
    for secret in (ticket, "frame-body", "user_1", "rm1", "p1"):
        assert secret not in caplog.text


SESSION = {
    "ownerId": "org_1",
    "memberId": "user_1",
    "runId": "session-11111111-1111-4111-8111-111111111111",
    "agentId": "session",
    "family": "session",
    "roomId": "",
}


def test_a_session_ticket_relays_the_offered_channel_to_the_runtime(mac, monkeypatch):
    from core.services import session_browser

    accepted, made = [], {}

    async def cdp_accept(channel, payload):
        accepted.append(payload)

    mac.cdp_accept = cdp_accept
    monkeypatch.setattr(
        session_browser,
        "relay",
        lambda claims: made.setdefault("channel", FakeChannel()) if claims["runId"] == SESSION["runId"] else None,
    )
    url = "/api/v1/internal/mac-cdp/" + mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session")
    with _client().websocket_connect(url) as ws:
        ws.send_text('{"id":1,"method":"Target.getTargets"}')
        assert ws.receive_text() == 'echo:{"id":1,"method":"Target.getTargets"}'
    assert accepted == [{"relay": True}] and mac.opened_with == [], "the Mac's Chrome is never opened"


def test_a_session_ticket_with_no_offered_channel_closes_4404(mac, monkeypatch):
    from core.services import session_browser

    monkeypatch.setattr(session_browser, "relay", lambda claims: None)
    url = "/api/v1/internal/mac-cdp/" + mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session")
    assert _closed_with(url)[0] == 4404
