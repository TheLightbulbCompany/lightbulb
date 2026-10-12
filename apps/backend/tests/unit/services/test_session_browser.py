"""A shared session's browser (spec 2026-10-11): offer -> lease -> sync -> open -> hold -> release."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from core.services import mac_browser, node_broker, session_browser
from core.services.desktop_execution import DesktopError

OWNER, MEMBER, DEVICE = "org_1", "user_1", "mac1"
SID = "11111111-1111-4111-8111-111111111111"
RUN = f"session-{SID}"
UPSTREAM = f"mac:{DEVICE}:{RUN}"
CHANNEL = "c" * 32
DOOR = "ws://10.0.3.7:8000/api/v1/internal/mac-cdp/T1"


def _answer(sync=None):
    return {
        "deviceId": DEVICE,
        "generation": "g1",
        "cdpUrl": DOOR,
        "deviceName": None,
        "humanInputAt": 0,
        "profileId": "prof1",
        "sync": sync,
    }


class Env:
    def __init__(self):
        self.row = {"ownerId": OWNER, "sessionId": SID, "humanUserId": MEMBER, "deviceId": DEVICE}
        self.acquired: list | Exception = [_answer({"syncId": "L1", "steps": [{"action": "load"}]})]
        self.released = {"sync": {"syncId": "L2", "steps": [{"action": "save"}]}, "cdpUrl": DOOR, "generation": "g1"}
        self.calls = []


@pytest.fixture
def env(monkeypatch):
    env = Env()
    monkeypatch.setattr(session_browser, "RENEW_SECONDS", 0.05)
    monkeypatch.setattr(session_browser, "POLL_S", 0)

    async def get_session(owner, sid):
        return copy.deepcopy(env.row) if (owner, sid) == (OWNER, SID) and env.row else None

    async def acquire(auth, device, *, budget_s=25):
        env.calls.append(("acquire", auth["runId"], device))
        if isinstance(env.acquired, Exception):
            raise env.acquired
        return env.acquired.pop(0) if env.acquired else _answer()

    async def release(auth, device, generation):
        env.calls.append(("release", generation))
        return env.released

    async def synced(auth, device, sync_id, ok):
        env.calls.append(("synced", sync_id, ok))

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        env.calls.append(("rpc", method, params["upstream"], [s["action"] for s in params["steps"]]))
        return {"ok": True}

    async def mark(owner, human, sid, where, *, announce):
        env.calls.append(("open", where))

    monkeypatch.setattr(session_browser.repo, "get_session", get_session)
    monkeypatch.setattr(session_browser.local_sessions, "_enrolled", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(mac_browser, "acquire", acquire)
    monkeypatch.setattr(mac_browser, "release", release)
    monkeypatch.setattr(mac_browser, "synced", synced)
    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    monkeypatch.setattr(session_browser, "_mark_open", mark)
    return env


def _offered(commands=("browser.cdp.v1", "browser.cast.v1")):
    node = node_broker.NodeSession(DEVICE, set(commands))
    node.on_offer = lambda *args: None
    node.resolve(
        {
            "type": "event",
            "event": "browser.cdp.offer",
            "payload": {"channel": CHANNEL, "sessionId": SID, "macBrowser": True},
        }
    )
    return node, node.cdp[CHANNEL]


def _out(node):
    frames = []
    while not node.cdp_out.empty():
        frames.append(node.cdp_out.get_nowait())
    return frames


async def _play_mac(node):
    """The Mac's half: answer the open with opened; returns what the backend sent."""
    for _ in range(200):
        frames = _out(node)
        if frames:
            for frame in frames:
                if frame["event"] == "browser.cdp.open":
                    node.resolve(
                        {
                            "type": "event",
                            "event": "browser.cdp.opened",
                            "payload": {"channel": frame["payload"]["channel"]},
                        }
                    )
            return frames
        await asyncio.sleep(0.01)
    return []


def _closed_by_mac(node):
    node.resolve({"type": "event", "event": "browser.cdp.close", "payload": {"channel": CHANNEL, "code": "CLOSED"}})


def _signal(node, kind):
    node.resolve({"type": "event", "event": f"browser.cdp.{kind}", "payload": {"channel": CHANNEL}})


async def _next(node, event):
    """The next frame of `event` the backend sends the Mac (waits up to 2 s)."""
    for _ in range(200):
        for frame in _out(node):
            if frame["event"] == event:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {event}")


async def _until(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def test_a_shared_session_gets_the_mac_with_the_workspace_sign_ins_then_saves_on_close(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    assert await _play_mac(node) == [
        {
            "type": "event",
            "event": "browser.cdp.open",
            "payload": {"channel": CHANNEL, "nodeId": DEVICE, "profileId": "prof1"},
        }
    ]
    await asyncio.sleep(0.12)  # past two renewals
    assert env.calls[:4] == [
        ("acquire", RUN, DEVICE),
        ("rpc", "browser.state", UPSTREAM, ["load"]),
        ("synced", "L1", True),
        ("open", "mac"),
    ]
    assert channel.local, "its CDP stays on the Mac"
    assert env.calls.count(("acquire", RUN, DEVICE)) >= 2, "renewed while open"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls[-4:] == [
        ("open", None),
        ("release", "g1"),
        ("rpc", "browser.state", UPSTREAM, ["save"]),
        ("synced", "L2", True),
    ]


async def test_idle_releases_and_saves_and_the_next_command_reacquires_before_it_runs(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    _signal(node, "idle")
    await _until(lambda: ("release", "g1") in env.calls)
    assert ("rpc", "browser.state", UPSTREAM, ["save"]) in env.calls, "the idle release saves"
    assert CHANNEL in node.cdp, "the connection stays open"
    calls = len(env.calls)
    await asyncio.sleep(0.12)
    assert ("acquire", RUN, DEVICE) not in env.calls[calls:], "no renewals while idle"
    env.acquired = [_answer({"syncId": "L3", "steps": [{"action": "save"}, {"action": "load"}]})]
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    assert ("rpc", "browser.state", UPSTREAM, ["save", "load"]) in env.calls, "saved before it loads"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls.count(("release", "g1")) == 2, "the idle release and the closing one"


async def test_a_failed_reacquire_answers_the_held_commands_code_and_keeps_the_connection(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    _signal(node, "idle")
    await _until(lambda: ("release", "g1") in env.calls)
    held = "This Mac's agent browser holds the sign-ins of the Acme workspace."
    env.acquired = DesktopError("browser_profile_held_by_another_workspace", 409, held)
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {
        "channel": CHANNEL,
        "ok": False,
        "code": "browser_profile_held_by_another_workspace",
        "message": held,
    }
    assert CHANNEL in node.cdp, "the next command tries again"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls.count(("release", "g1")) == 1, "nothing held, nothing released at the close"


async def test_an_active_that_never_lost_its_lease_is_answered_at_once(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    _signal(node, "active")  # a command after a short idle the Mac counted, but the lease is still held
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    assert not any(call[0] == "release" for call in env.calls)
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_an_unshared_session_is_refused_with_not_shared(env):
    env.row["unshared"] = True
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node) == [
        {
            "type": "event",
            "event": "browser.cdp.close",
            "payload": {"channel": CHANNEL, "code": "not_shared", "message": session_browser.NOT_SHARED},
        }
    ]
    assert env.calls == []


@pytest.mark.parametrize("change", [{"humanUserId": "user_2"}, {"deviceId": "mac2"}, {"endedAt": 1}, {"removedAt": 1}])
async def test_a_session_that_is_not_this_macs_running_shared_one_is_not_shared(env, change):
    env.row.update(change)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node)[-1]["payload"]["code"] == "not_shared"


async def test_sharing_switched_off_for_the_mac_is_not_shared(env, monkeypatch):
    from core.services.messaging import ApiProblem

    monkeypatch.setattr(
        session_browser.local_sessions,
        "_enrolled",
        AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")),
    )
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node)[-1]["payload"]["code"] == "not_shared"


async def test_another_workspaces_profile_is_refused_with_its_message(env):
    held = "This Mac's agent browser holds the sign-ins of the Acme workspace. Unpair it from Acme first, or use the cloud browser."
    env.acquired = DesktopError("browser_profile_held_by_another_workspace", 409, held)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node) == [
        {
            "type": "event",
            "event": "browser.cdp.close",
            "payload": {"channel": CHANNEL, "code": "browser_profile_held_by_another_workspace", "message": held},
        }
    ]
    assert not any(call[0] == "release" for call in env.calls), "nothing was leased"


async def test_browser_off_is_refused(env):
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"]["code"] == "browser_off"


async def test_unsharing_while_open_closes_the_channel_and_still_releases(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    env.row["unshared"] = True
    await asyncio.wait_for(served, 2)
    assert {
        "type": "event",
        "event": "browser.cdp.close",
        "payload": {"channel": CHANNEL, "code": "not_shared", "message": session_browser.NOT_SHARED},
    } in _out(node)
    assert ("release", "g1") in env.calls


async def test_a_starting_mac_is_polled_inside_the_offer_budget(env, monkeypatch):
    env.acquired = [mac_browser.STARTING, mac_browser.STARTING, _answer()]
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    assert (await _play_mac(node))[0]["event"] == "browser.cdp.open"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls[:3] == [("acquire", RUN, DEVICE)] * 3


async def test_a_mac_without_the_cdp_command_is_refused(env):
    node, channel = _offered(commands=("browser.cast.v1",))
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node)[-1]["payload"]["code"] == "browser_off"
    assert env.calls == []


async def test_an_unexpected_failure_closes_the_offer_and_start_never_raises(env, monkeypatch, caplog):
    monkeypatch.setattr(session_browser, "shared_row", AsyncMock(side_effect=KeyError("secret-ticket")))
    node, channel = _offered()
    session_browser.start(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    await _until(lambda: channel.closed.is_set())
    assert _out(node)[-1]["payload"]["code"] == "CLOSED"
    assert "secret-ticket" not in caplog.text and "KeyError" in caplog.text


async def test_a_close_lost_behind_a_full_signal_queue_still_ends_the_hold(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    await _until(lambda: ("open", "mac") in env.calls)
    # The hold waits on the queue it already took; a new full one cannot carry "closed" either.
    channel.signals = asyncio.Queue(1)
    channel.signals.put_nowait("active")
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert ("release", "g1") in env.calls


async def test_a_failed_renewal_keeps_the_connection(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    env.acquired = DesktopError("mac_browser_busy", 409)
    calls = len(env.calls)
    await _until(lambda: ("acquire", RUN, DEVICE) in env.calls[calls:])
    await asyncio.sleep(0.12)
    assert CHANNEL in node.cdp and not any(f["event"] == "browser.cdp.leased" for f in _out(node))
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert not any(call[0] == "release" for call in env.calls), "the refused lease is not held"


async def test_a_starting_renewal_keeps_the_lease_and_the_connection(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    env.acquired = [mac_browser.STARTING] * 50
    await asyncio.sleep(0.12)
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert ("release", "g1") in env.calls


async def test_two_syncs_of_one_session_never_run_at_once(env, monkeypatch):
    running, overlaps = 0, []

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        nonlocal running
        running += 1
        overlaps.append(running)
        await asyncio.sleep(0.02)
        running -= 1
        return {"ok": True}

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    auth = session_browser.auth_for(OWNER, MEMBER, SID)
    answer = {"cdpUrl": DOOR, "generation": "g1", "sync": {"syncId": "S", "steps": [{"action": "save"}]}}
    await asyncio.gather(session_browser._sync(auth, DEVICE, answer), session_browser._sync(auth, DEVICE, answer))
    assert overlaps == [1, 1]


async def test_a_refused_renewal_drops_the_lease_so_the_next_command_reacquires(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    env.acquired = DesktopError("mac_browser_busy", 409)
    calls = len(env.calls)
    await _until(lambda: ("acquire", RUN, DEVICE) in env.calls[calls:])
    await asyncio.sleep(0.12)
    renewals = env.calls.count(("acquire", RUN, DEVICE))
    await asyncio.sleep(0.12)
    assert env.calls.count(("acquire", RUN, DEVICE)) == renewals, "no renewals once the lease is gone"
    held = "This Mac's agent browser holds the sign-ins of the Acme workspace."
    env.acquired = DesktopError("browser_profile_held_by_another_workspace", 409, held)
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {
        "channel": CHANNEL,
        "ok": False,
        "code": "browser_profile_held_by_another_workspace",
        "message": held,
    }
    assert env.calls.count(("acquire", RUN, DEVICE)) == renewals + 1, "the command re-acquired"
    env.acquired = [_answer({"syncId": "L4", "steps": [{"action": "load"}]})]
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    assert ("synced", "L4", True) in env.calls
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls.count(("release", "g1")) == 1, "only the re-acquired lease is released"
