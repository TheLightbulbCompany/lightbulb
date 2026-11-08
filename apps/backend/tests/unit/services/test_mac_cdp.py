"""The Mac CDP ticket: mint/redeem/claims, and the door's authority re-check (spec §5)."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from core.config import settings
from core.services import mac_cdp
from core.services.connection_service import DesktopTargetStale
from core.services.desktop_execution import DesktopError

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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(mac_cdp.task_address, "own_address", lambda: ADDRESS)
    monkeypatch.setattr(mac_cdp, "_clock", lambda: 1_000_000)
    mac_cdp._redeemed.clear()


def test_ticket_round_trip_claims():
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM))
    assert claims == {
        "purpose": "mac-cdp-v1",
        "use": "agent",
        "family": "room",
        "ownerId": "org_1",
        "memberId": "user_1",
        "deviceId": "mac1",
        "generation": "g1",
        "address": ADDRESS,
        "profileId": "p1",
        "runId": "r1",
        "agentId": "a1",
        "roomId": "rm1",
        "nonce": claims["nonce"],
        "iat": 1_000_000,
        "exp": 1_000_060,
    }
    assert mac_cdp.auth_from_claims(claims) == ROOM


def test_ticket_expired(monkeypatch):
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)
    monkeypatch.setattr(mac_cdp, "_clock", lambda: 1_000_000 + 60 + 5)
    mac_cdp.redeem(ticket)  # within the 5 s skew
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)  # iat = now
    monkeypatch.setattr(mac_cdp, "_clock", lambda: 1_000_065 + 66)
    with pytest.raises(mac_cdp.TicketError) as exc:
        mac_cdp.redeem(ticket)
    assert exc.value.code == "expired"


def test_ticket_single_use():
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)
    mac_cdp.redeem(ticket)
    with pytest.raises(mac_cdp.TicketError) as exc:
        mac_cdp.redeem(ticket)
    assert exc.value.code == "replayed"


def test_ticket_minted_for_another_task_is_refused():
    ticket = mac_cdp.mint(target=TARGET | {"taskAddress": "http://10.0.3.8:8000"}, profile_id="p1", auth=ROOM)
    with pytest.raises(mac_cdp.TicketError) as exc:
        mac_cdp.redeem(ticket)
    assert exc.value.code == "wrong_task"


def test_tampered_ticket_is_invalid(monkeypatch):
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)
    for bad in (ticket[:-4] + "AAAA", "not-a-ticket", ""):
        with pytest.raises(mac_cdp.TicketError) as exc:
            mac_cdp.redeem(bad)
        assert exc.value.code == "invalid"
    forged = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)) | {"use": "state", "nonce": "n2"}
    with pytest.raises(mac_cdp.TicketError):
        mac_cdp.redeem(mac_cdp._cipher().encrypt(json.dumps(forged).encode()).decode())
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "other")
    with pytest.raises(mac_cdp.TicketError):
        mac_cdp.redeem(ticket)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "")
    with pytest.raises(mac_cdp.TicketError):
        mac_cdp.redeem(ticket)


def test_agent_ticket_requires_auth():
    with pytest.raises(TypeError):
        mac_cdp.mint(target=TARGET, profile_id="p1")
    with pytest.raises(ValueError):
        mac_cdp.mint(target=TARGET, profile_id="p1", auth={})
    with pytest.raises(ValueError):
        mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM | {"ownerId": "org_2"})


def test_door_url_uses_task_host_and_port():
    assert mac_cdp.door_url(TARGET, "TKT") == "ws://10.0.3.7:8000/api/v1/internal/mac-cdp/TKT"


def _claims(**kw):
    return mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)) | kw


async def test_check_refuses_disabled_owner(monkeypatch):
    monkeypatch.setattr(mac_cdp.desktop_execution, "submissions_enabled", lambda owner: False)
    resolve = AsyncMock(return_value=TARGET)
    monkeypatch.setattr(mac_cdp.connection_service, "resolve_desktop_target", resolve)
    with pytest.raises(DesktopError) as exc:
        await mac_cdp.check(_claims())
    assert exc.value.code == "desktop_disabled"
    resolve.assert_not_awaited()


async def test_check_refuses_generation_change(monkeypatch):
    monkeypatch.setattr(mac_cdp.desktop_execution, "submissions_enabled", lambda owner: True)
    monkeypatch.setattr(
        mac_cdp.connection_service, "resolve_desktop_target", AsyncMock(side_effect=DesktopTargetStale("mac1"))
    )
    with pytest.raises(DesktopError) as exc:
        await mac_cdp.check(_claims())
    assert exc.value.code == "desktop_generation_stale"


async def test_check_reauthorizes_the_run_and_the_task(monkeypatch):
    from core.services import mac_browser

    holder = AsyncMock()
    monkeypatch.setattr(mac_browser, "check_holder", holder)
    monkeypatch.setattr(mac_cdp.desktop_execution, "submissions_enabled", lambda owner: True)
    resolve = AsyncMock(return_value=TARGET)
    monkeypatch.setattr(mac_cdp.connection_service, "resolve_desktop_target", resolve)
    reauth = AsyncMock()
    monkeypatch.setattr(mac_cdp.desktop_execution, "_reauthorize", reauth)
    assert await mac_cdp.check(_claims()) == TARGET
    resolve.assert_awaited_once_with("org_1", "user_1", "mac1", "g1", "browser.cdp.v1")
    reauth.assert_awaited_once_with(ROOM)
    holder.assert_awaited_once_with("org_1", "mac1", "p1")  # the Mac still holds this workspace's profile
    holder.side_effect = DesktopError("browser_profile_held_by_another_workspace", 403)
    with pytest.raises(DesktopError):
        await mac_cdp.check(_claims())
    holder.side_effect = None
    resolve.return_value = TARGET | {"taskAddress": "http://10.0.3.8:8000"}
    with pytest.raises(DesktopError) as exc:
        await mac_cdp.check(_claims())
    assert exc.value.code == "desktop_task_moved"


class _Channel:
    def __init__(self):
        self.close_code = None
        self.queue = asyncio.Queue()

    async def next(self):
        return await self.queue.get()


class _Session:
    def __init__(self):
        self.channel = _Channel()

    async def cdp_open(self, profile_id, notice=None):
        return self.channel

    def cdp_close(self, channel, code="CLOSED"):
        pass


class _Socket:
    def __init__(self):
        self.closes = []
        self.inbox = asyncio.Queue()

    async def accept(self):
        pass

    async def receive(self):
        return await self.inbox.get()

    async def close(self, code, reason=""):
        self.closes.append(code)


async def test_serve_starts_mac_live_and_its_end_does_not_close_the_door(monkeypatch):
    session, socket, started = _Session(), _Socket(), []

    async def live_run(claims, s, channel):
        started.append((claims["runId"], s, channel))  # returns at once, like a non-castable run

    monkeypatch.setattr(mac_cdp.mac_live, "run", live_run)
    monkeypatch.setattr(mac_cdp, "check", AsyncMock(return_value=TARGET))
    monkeypatch.setattr(mac_cdp.desktop_execution, "_session", lambda target: session)
    ticket = mac_cdp.mint(target=TARGET, profile_id="p1", auth=ROOM)
    door = asyncio.create_task(mac_cdp.serve(socket, ticket))
    for _ in range(50):
        await asyncio.sleep(0)
    assert started == [("r1", session, session.channel)]
    assert not door.done() and socket.closes == []
    session.channel.queue.put_nowait(None)  # the Mac closes the channel normally
    await door
    assert socket.closes == [1000]


def test_a_voice_runs_ticket_redeems_like_a_room_runs():
    """A spoken turn (family "call") reaches its requester's Mac browser through the same door."""
    call = ROOM | {"family": "call"}
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=call))
    assert claims is not None and mac_cdp.auth_from_claims(claims) == call


SESSION = {
    "ownerId": "org_1",
    "memberId": "user_1",
    "runId": "session-11111111-1111-4111-8111-111111111111",
    "agentId": "session",
    "family": "session",
    "roomId": "",
}


def test_a_session_runs_ticket_round_trips():
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=SESSION))
    assert claims["family"] == "session" and claims["use"] == "agent"
    assert mac_cdp.auth_from_claims(claims) == SESSION


def test_a_relay_ticket_is_for_the_session_family_only():
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session"))
    assert claims["use"] == "session" and mac_cdp.auth_from_claims(claims) == SESSION
    with pytest.raises(ValueError):
        mac_cdp.mint(target=TARGET, profile_id="cloud", auth=ROOM, use="session")


async def test_a_relay_needs_no_browser_grant_and_no_mac_profile(monkeypatch):
    seen = {}

    async def resolve(owner, member, device, generation, command):
        seen["command"] = command
        return TARGET

    monkeypatch.setattr(mac_cdp.connection_service, "resolve_desktop_target", resolve)
    monkeypatch.setattr(mac_cdp.desktop_execution, "submissions_enabled", lambda owner: True)
    monkeypatch.setattr(mac_cdp.desktop_execution, "_reauthorize", AsyncMock())
    from core.services import mac_browser

    monkeypatch.setattr(
        mac_browser, "check_holder", AsyncMock(side_effect=AssertionError("no Mac profile is involved"))
    )
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session"))
    assert await mac_cdp.check(claims) == TARGET and seen["command"] is None
