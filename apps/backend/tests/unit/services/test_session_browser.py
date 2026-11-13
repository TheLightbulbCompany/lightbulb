"""A shared session's browser: offer -> lease -> sync -> open -> hold -> release."""

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
    monkeypatch.setattr(session_browser.store, "ensure_workspace_id", AsyncMock(return_value="ws-1"))
    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: None)
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


async def test_a_mac_without_the_cdp_command_goes_to_the_cloud(env, cloud):
    node, channel = _offered(commands=("browser.cast.v1",))
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _relayed(env, cloud, node, channel)
    assert not any(call[0] == "acquire" for call in env.calls)
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


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


async def test_a_failed_open_mark_never_closes_the_connection(monkeypatch):
    from botocore.exceptions import ClientError

    async def throttled(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")

    monkeypatch.setattr(session_browser.store, "run_in_thread", throttled)
    monkeypatch.setattr(session_browser.store, "_table", lambda: AsyncMock())
    await session_browser._mark_open(OWNER, MEMBER, SID, "mac", announce=True)  # does not raise


async def test_a_client_gone_during_acquire_releases_and_opens_nothing(env):
    node, channel = _offered()
    node.cdp_close(channel, "CLOSED")
    _out(node)
    await asyncio.wait_for(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True), 2)
    assert [f for f in _out(node) if f["event"] == "browser.cdp.open"] == []
    assert ("release", "g1") in env.calls


class FakeNode:
    def __init__(self):
        self.sets = []

    def cast_set(self, channel, view, thumb):
        self.sets.append((view, thumb))


class FakeCastChannel:
    def __init__(self):
        self.cast, self.cast_ready = {}, asyncio.Event()


async def test_frames_flow_only_while_someone_watches(env, monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(session_browser, "_clock", lambda: now["t"])
    monkeypatch.setattr(session_browser.mac_live, "FRAME_POLL_S", 0.01)
    node = FakeNode()
    watching = asyncio.create_task(session_browser._watch_mac(OWNER, SID, node, object()))
    await asyncio.sleep(0.05)
    assert node.sets == [], "nobody watches: no cast"
    env.row["browserWatchedAt"] = now["t"]
    await asyncio.sleep(0.05)
    assert node.sets == [(False, True)], "the thumb tier only"
    now["t"] += session_browser.mac_live.WANTED_S
    await asyncio.sleep(0.05)
    assert node.sets[-1] == (False, False)
    watching.cancel()


async def test_a_cast_frame_goes_to_the_session_frames_channel_as_an_image(env, monkeypatch):
    published = []

    class Publisher:
        async def publish_session_frames(self, workspace, event):
            published.append((workspace, event))

    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: Publisher())
    monkeypatch.setattr(session_browser.store, "ensure_workspace_id", AsyncMock(return_value="ws-1"))
    session_browser.session_screen._spent.clear()
    channel = FakeCastChannel()
    publishing = asyncio.create_task(session_browser._publish(OWNER, SID, channel))
    channel.cast["thumb"] = {"jpegBase64": "/9j/4AAQ", "w": 1280, "h": 800, "seq": 7}
    channel.cast_ready.set()
    await asyncio.sleep(0.01)
    publishing.cancel()
    ((workspace, event),) = published
    assert workspace == "ws-1" and event["type"] == "workspace.session.screen"
    assert event["payload"] == {"sessionId": SID, "kind": "image", "data": "/9j/4AAQ", "w": 1280, "h": 800, "seq": 7}


async def test_only_people_who_may_type_into_the_session_may_drive_its_browser(env, monkeypatch):
    monkeypatch.setattr(session_browser.session_screen, "_still_a_member", AsyncMock())
    assert await session_browser.may_type(OWNER, MEMBER, RUN), "its own person"
    assert not await session_browser.may_type(OWNER, "user_2", RUN), "a watcher"
    env.row["allowedSenders"] = ["user_2"]
    assert await session_browser.may_type(OWNER, "user_2", RUN), "let in by its person"
    env.row["unshared"] = True
    assert not await session_browser.may_type(OWNER, MEMBER, RUN)
    assert not await session_browser.may_type(OWNER, MEMBER, "r1"), "not a session run"


async def test_a_session_that_stops_being_shared_stops_its_frames_at_once(env, monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(session_browser, "_clock", lambda: now["t"])
    monkeypatch.setattr(session_browser.mac_live, "FRAME_POLL_S", 0.01)
    node = FakeNode()
    env.row["browserWatchedAt"] = now["t"]
    watching = asyncio.create_task(session_browser._watch_mac(OWNER, SID, node, object()))
    await asyncio.sleep(0.05)
    assert node.sets == [(False, True)]
    env.row["unshared"] = True
    await asyncio.sleep(0.05)
    assert node.sets[-1] == (False, False), "the stamp is still fresh, the session is not shared"
    watching.cancel()


def _auth(user):
    from core.auth import AuthContext

    return AuthContext(user_id=user, org_id=OWNER)


OPEN = {"browserOpen": {"where": "mac", "until": 2**40}}


async def test_the_hold_ending_stops_its_frames_and_input(env, monkeypatch):
    node, channel = _offered()
    cancelled, seen = [], []

    async def side(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(args[1])

    async def lease_loop(auth, device, node, channel, leased):
        await asyncio.sleep(0)  # the side tasks start
        seen.append(dict(session_browser.mac_live._live))

    monkeypatch.setattr(session_browser, "_watch_mac", side)
    monkeypatch.setattr(session_browser, "_publish", side)
    monkeypatch.setattr(session_browser, "_lease_loop", lease_loop)
    await session_browser._hold_mac(session_browser.auth_for(OWNER, MEMBER, SID), DEVICE, node, channel, {})
    await asyncio.sleep(0)
    assert (OWNER, RUN) in seen[0]
    assert (OWNER, RUN) not in session_browser.mac_live._live
    assert cancelled == [SID, SID]


async def test_a_failed_workspace_read_does_not_stop_frames(env, monkeypatch):
    published = []

    class Publisher:
        async def publish_session_frames(self, workspace, event):
            published.append(workspace)

    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: Publisher())
    monkeypatch.setattr(
        session_browser.store, "ensure_workspace_id", AsyncMock(side_effect=[RuntimeError("ddb"), "ws-1", "ws-1"])
    )
    session_browser.session_screen._spent.clear()
    channel = FakeCastChannel()
    publishing = asyncio.create_task(session_browser._publish(OWNER, SID, channel))
    for seq in (1, 2):
        channel.cast["thumb"] = {"jpegBase64": "/9j/4AAQ", "w": 1280, "h": 800, "seq": seq}
        channel.cast_ready.set()
        await asyncio.sleep(0.01)
    publishing.cancel()
    assert published == ["ws-1"], "the first frame is dropped, the task survives and the next goes out"


async def test_a_pending_frame_is_dropped_once_the_session_is_unshared(env, monkeypatch):
    published = []

    class Publisher:
        async def publish_session_frames(self, workspace, event):
            published.append(event)

    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: Publisher())
    session_browser.session_screen._spent.clear()
    channel = FakeCastChannel()
    env.row["unshared"] = True
    publishing = asyncio.create_task(session_browser._publish(OWNER, SID, channel))
    channel.cast["thumb"] = {"jpegBase64": "/9j/4AAQ", "w": 1280, "h": 800, "seq": 1}
    channel.cast_ready.set()
    await asyncio.sleep(0.01)
    publishing.cancel()
    assert published == []


@pytest.fixture
def cloud(env, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://x.appsync-api.us-east-1.amazonaws.com/event")
    target = {
        "taskAddress": "http://10.0.3.7:8000",
        "ownerId": OWNER,
        "memberId": MEMBER,
        "deviceId": DEVICE,
        "generation": "g1",
        "commands": [],
    }
    monkeypatch.setattr(session_browser, "_native_target", AsyncMock(return_value=target))
    answers = [{"status": "starting"}, {"status": "attached"}]
    seen = []

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        seen.append((method, params))
        if method == "browser.attach":
            return answers.pop(0) if answers else {"status": "attached"}  # a renew after idle
        return {"ok": True}

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    yield seen
    session_browser._relays.clear()
    session_browser._cloud.clear()


async def _relayed(env, seen, node, channel):
    """The cloud is attached and the session's channel waits for its door; the door then joins it
    (the real cdp_accept) and the Mac answers its open."""
    await _until(
        lambda: (
            (OWNER, RUN) in session_browser._relays
            and any(m == "browser.attach" and "doorUrl" in (p or {}) for m, p in seen)
        )
    )
    assert ("open", "cloud") not in env.calls, "not announced before a door has the channel"
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is channel
    accepted = asyncio.create_task(node.cdp_accept(channel, {"relay": True}))  # the door's half
    await asyncio.sleep(0.15)
    assert ("open", "cloud") not in env.calls, "not announced before the Mac opened the relay"
    (opened,) = await _play_mac(node)
    assert opened["event"] == "browser.cdp.open" and opened["payload"]["relay"] is True
    await asyncio.wait_for(accepted, 2)
    await _until(lambda: ("open", "cloud") in env.calls)


async def test_browser_off_routes_the_session_to_the_cloud_browser(env, cloud):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is None, "one door only: _relayed already joined it"
    attaches = [params for method, params in cloud if method == "browser.attach" and "doorUrl" in params]
    assert len(attaches) == 2 and attaches[0]["doorUrl"] != attaches[1]["doorUrl"], "a fresh ticket each time"
    assert attaches[1]["doorUrl"].startswith("ws://10.0.3.7:8000/api/v1/internal/mac-cdp/")
    assert (attaches[1]["runId"], attaches[1]["frameChannel"]) == (RUN, f"/ws/ws-1/session-frames/{SID}")
    assert not any(call[0] in ("acquire", "release") for call in env.calls), "the Mac browser is never leased"
    await _until(lambda: ("health", {}) in cloud)  # the runtime is kept awake while it browses
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert (OWNER, RUN) not in session_browser._relays
    assert env.calls[-1] == ("open", None)


async def test_after_idle_the_next_command_waits_for_the_bridge_to_renew(env, cloud):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    _signal(node, "idle")
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    assert ("browser.attach", {"runId": RUN, "renew": True}) in cloud
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


def _renews(seen):
    return sum(1 for m, p in seen if m == "browser.attach" and p == {"runId": RUN, "renew": True})


async def test_an_idle_cloud_session_keeps_renewing_its_lease_for_30_minutes_then_rests(env, cloud, monkeypatch):
    """Renewing before the 300 s lease lapses keeps the cloud generation (browser_control._claim's
    own-lease branch); a lapse would re-claim and revoke the relay's connection."""
    now = {"t": 1000.0}
    monkeypatch.setattr(session_browser, "_clock", lambda: now["t"])
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    marks = env.calls.count(("open", "cloud"))
    _signal(node, "idle")
    await _until(lambda: env.calls.count(("open", "cloud")) > marks)  # the idle was taken at t=1000
    now["t"] += 29 * 60  # still inside the hold: the timer keeps renewing
    start = _renews(cloud)
    await _until(lambda: _renews(cloud) >= start + 3)
    assert sum(1 for m, p in cloud if m == "browser.attach" and "doorUrl" in p) == 2, "never a fresh attach"
    now["t"] += 2 * 60  # 31 idle minutes: renewals stop, the runtime is still woken
    await asyncio.sleep(0.12)  # let a renew already in flight land
    stopped, health = _renews(cloud), cloud.count(("health", {}))
    await _until(lambda: cloud.count(("health", {})) >= health + 2)
    assert _renews(cloud) == stopped
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_an_active_after_a_short_idle_answers_at_once_on_the_held_lease(env, cloud, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(session_browser, "_clock", lambda: now["t"])
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    _signal(node, "idle")
    now["t"] += 5 * 60
    _signal(node, "active")
    assert (await _next(node, "browser.cdp.leased"))["payload"] == {"channel": CHANNEL, "ok": True}
    assert sum(1 for m, p in cloud if m == "browser.attach" and "doorUrl" in p) == 2, "no re-attach"
    now["t"] += 60 * 60  # active again: the quiet clock restarted, so renewals continue
    start = _renews(cloud)
    await _until(lambda: _renews(cloud) >= start + 2)
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_a_session_set_to_cloud_goes_there_with_browser_on(env, cloud):
    env.row["browserPlace"] = "cloud"
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _relayed(env, cloud, node, channel)
    assert any(method == "browser.attach" for method, _ in cloud)
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_a_cloud_that_does_not_start_is_cloud_unavailable(env, cloud, monkeypatch):
    monkeypatch.setattr(session_browser, "OFFER_BUDGET_S", 0.05)

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        return {"status": "starting"}

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"] == {
        "channel": CHANNEL,
        "code": "cloud_unavailable",
        "message": session_browser.CLOUD_MESSAGE,
    }
    assert (OWNER, RUN) not in session_browser._relays


async def test_unsharing_a_session_on_the_cloud_closes_its_door(env, cloud):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    env.row["unshared"] = True
    await asyncio.wait_for(served, 2)
    assert channel.closed.is_set(), "the door's pump ends with the channel"
    assert _out(node)[-1]["payload"]["code"] == "not_shared"
    assert (OWNER, RUN) not in session_browser._relays, "no door can join it afterwards"


def _offer_another(node, channel_id):
    node.resolve(
        {
            "type": "event",
            "event": "browser.cdp.offer",
            "payload": {"channel": channel_id, "sessionId": SID, "macBrowser": True},
        }
    )
    return node.cdp[channel_id]


async def test_one_cloud_connection_per_session_a_second_is_refused_never_overwritten(env, cloud):
    node, first = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, first, SID, False))
    await _until(lambda: (OWNER, RUN) in session_browser._relays)
    second = _offer_another(node, "d" * 32)
    await asyncio.wait_for(session_browser.serve(OWNER, MEMBER, DEVICE, node, second, SID, False), 2)
    assert _out(node)[-1]["payload"] == {
        "channel": "d" * 32,
        "code": "cloud_busy",
        "message": session_browser.CLOUD_BUSY,
    }
    await _relayed(env, cloud, node, first)  # the first door claims the first channel
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert (OWNER, RUN) not in session_browser._cloud

    env.calls.clear()
    cloud.clear()
    third = _offer_another(node, "e" * 32)
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, third, SID, False))
    await _relayed(env, cloud, node, third)  # once the first closed, a new offer works
    node.resolve({"type": "event", "event": "browser.cdp.close", "payload": {"channel": "e" * 32, "code": "CLOSED"}})
    await asyncio.wait_for(served, 2)


def test_another_workspaces_ticket_never_finds_the_relay():
    channel = object()
    session_browser._relays[(OWNER, RUN)] = channel
    try:
        assert session_browser.relay({"ownerId": "org_2", "runId": RUN}) is None
        assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is channel
    finally:
        session_browser._relays.clear()


async def test_a_door_that_never_joins_is_cloud_unavailable(env, cloud, monkeypatch):
    monkeypatch.setattr(session_browser, "DOOR_WAIT_S", 0.05)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"]["code"] == "cloud_unavailable"
    assert ("open", "cloud") not in env.calls


async def test_the_runtime_is_woken_before_any_ticket_is_minted(env, cloud, monkeypatch):
    real_mint = session_browser.mac_cdp.mint

    def mint(**kwargs):
        cloud.append(("mint", None))
        return real_mint(**kwargs)

    monkeypatch.setattr(session_browser.mac_cdp, "mint", mint)
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    assert [m for m, _ in cloud][:2] == ["health", "mint"]
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_a_runtime_that_does_not_wake_is_cloud_unavailable(env, cloud, monkeypatch):
    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    monkeypatch.setattr(session_browser.mac_cdp, "mint", lambda **kw: pytest.fail("minted"))
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"]["code"] == "cloud_unavailable"


async def test_a_relay_the_mac_never_opens_is_never_announced(env, cloud, monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_OPEN_TIMEOUT_S", 0.05)
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _until(lambda: (OWNER, RUN) in session_browser._relays and any(m == "browser.attach" for m, _ in cloud))
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is channel
    with pytest.raises(node_broker.CdpClosed):
        await node.cdp_accept(channel, {"relay": True})  # the door's half; the Mac never says opened
    await asyncio.wait_for(served, 2)
    assert ("open", "cloud") not in env.calls


async def test_unsharing_a_cloud_session_stops_its_cast_at_once(env, cloud, monkeypatch):
    monkeypatch.setattr(session_browser.mac_live, "SET_EVERY_S", 0.01)
    env.row["browserWatchedAt"] = 2**40  # watched: the cast is asked for
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env, cloud, node, channel)
    await _until(lambda: ("browser.frames", {"runId": RUN, "ttl": session_browser.mac_live.WANTED_S}) in cloud)
    env.row["unshared"] = True
    await asyncio.wait_for(served, 2)
    assert ("browser.frames", {"runId": RUN, "ttl": 0}) in cloud
    asks = [p for m, p in cloud if m == "browser.frames"]
    after = asks[asks.index({"runId": RUN, "ttl": 0}) :]
    assert all(ask["ttl"] == 0 for ask in after), "no cast asked for after the stop"


class PlaceTable:
    """A one-row META table whose update_item honours `revision = :rev`; `bump` simulates a racing registration."""

    def __init__(self, row, bumps=0):
        self.row, self.bumps, self.calls = row, bumps, 0

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        from botocore.exceptions import ClientError

        self.calls += 1
        if self.bumps:
            self.bumps -= 1
            self.row["revision"] = f"raced-{self.calls}"
        if ExpressionAttributeValues[":rev"] != self.row["revision"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.row["browserPlace"] = ExpressionAttributeValues[":w"]
        self.row["revision"] = ExpressionAttributeValues[":new"]


@pytest.fixture
def place_env(monkeypatch):
    row = {"revision": "r0"}
    table = PlaceTable(row)

    async def run_in_thread(fn, **kwargs):
        return fn(**kwargs)

    monkeypatch.setattr(session_browser.store, "run_in_thread", run_in_thread)
    monkeypatch.setattr(session_browser.store, "_table", lambda: table)
    monkeypatch.setattr(session_browser.repo, "get_session", AsyncMock(side_effect=lambda o, s: dict(row)))
    return table


async def test_use_browser_sets_where_the_next_connection_goes(place_env):
    from core.services.messaging import ApiProblem

    assert await session_browser.set_place(OWNER, SID, "cloud") == {
        "sessionId": SID,
        "browser": "cloud",
        "note": session_browser.PLACE_NOTE,
    }
    assert "restart the lightbulb-browser MCP server" in session_browser.PLACE_NOTE
    assert place_env.row["browserPlace"] == "cloud"
    assert place_env.row["revision"] != "r0"
    with pytest.raises(ApiProblem):
        await session_browser.set_place(OWNER, SID, "elsewhere")


async def test_use_browser_retries_when_a_registration_bumps_the_revision(place_env):
    place_env.bumps = 1
    await session_browser.set_place(OWNER, SID, "cloud")
    assert place_env.calls == 2
    assert place_env.row["browserPlace"] == "cloud"
    assert place_env.row["revision"] not in ("r0", "raced-1")


async def test_use_browser_gives_a_retryable_error_when_the_row_keeps_changing(place_env):
    from core.services.messaging import ApiProblem

    place_env.bumps = 99
    with pytest.raises(ApiProblem) as caught:
        await session_browser.set_place(OWNER, SID, "cloud")
    assert caught.value.error.retryable
    assert "browserPlace" not in place_env.row
