"""mac_live: viewer stamps drive the Mac's cast on/off; its frames go to the room's AppSync channels."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.services import mac_live
from core.services.node_broker import CdpClosed

OWNER = "org_1"
ROOM = "22222222-2222-4222-8222-222222222222"
CLAIMS = {"ownerId": OWNER, "runId": "r1", "agentId": "a1", "family": "room", "roomId": ROOM}


class FakeChannel:
    def __init__(self):
        self.closed = asyncio.Event()
        self.cast: dict = {}
        self.cast_ready = asyncio.Event()

    def frame(self, tier, seq):  # what node_broker._cast_frame does
        self.cast[tier] = {"jpegBase64": f"J{seq}", "w": 800, "h": 600, "seq": seq}
        self.cast_ready.set()


class FakeSession:
    def __init__(self, can_cast=True):
        self.can_cast = can_cast
        self.sets, self.inputs = [], []
        self.set_error = None

    def cast_set(self, channel, view, thumb):
        if self.set_error:
            raise self.set_error
        self.sets.append((view, thumb))

    def cast_input(self, channel, events):
        self.inputs.append(events)


class FakePublisher:
    def __init__(self):
        self.calls = []
        self.gate = None

    async def publish_room(self, room_id, event, *, frames=False):
        self.calls.append((room_id, event, frames))
        if self.gate is not None:
            await self.gate.wait()
        return True


@pytest.fixture
def env(monkeypatch):
    now = {"t": 1_000_000.0}
    stamps: dict = {}
    publisher = FakePublisher()
    monkeypatch.setattr(mac_live, "FRAME_POLL_S", 0)
    monkeypatch.setattr(mac_live, "_clock", lambda: now["t"])

    async def get_row(owner_key, consistent=False):
        # One strongly consistent read of the workspace's browser row per tick, both tiers from it.
        assert owner_key.startswith(mac_live.container_repo.BROWSER_KEY_PREFIX) and consistent
        return dict(stamps)

    monkeypatch.setattr(mac_live.container_repo, "get_by_owner_id", AsyncMock(side_effect=get_row))
    monkeypatch.setattr(mac_live.live_events, "get_live_event_publisher", lambda: publisher)
    mac_live._live.clear()
    return now, stamps, publisher


async def _spin(n=50):
    for _ in range(n):
        await asyncio.sleep(0)


async def _start(session, channel, claims=CLAIMS):
    task = asyncio.create_task(mac_live.run(claims, session, channel))
    await _spin()
    return task


async def test_non_room_or_uncastable_is_not_registered(env):
    channel = FakeChannel()
    await mac_live.run(CLAIMS | {"family": "paperclip", "roomId": ""}, FakeSession(), channel)
    await mac_live.run(CLAIMS | {"roomId": ""}, FakeSession(), channel)
    await mac_live.run(CLAIMS, FakeSession(can_cast=False), channel)
    assert mac_live._live == {}


async def test_fresh_view_stamp_sets_once_then_every_ten_seconds(env):
    now, stamps, _ = env
    session, channel = FakeSession(), FakeChannel()
    task = await _start(session, channel)
    assert session.sets == []  # nobody watching: nothing sent
    stamps["mac_view_at"] = now["t"] - 1
    stamps["mac_thumb_at"] = now["t"] - 60  # stale: not wanted
    await _spin()
    assert session.sets == [(True, False)]
    now["t"] += 5
    await _spin()
    assert session.sets == [(True, False)]
    now["t"] += 5
    stamps["mac_view_at"] = now["t"]
    await _spin()
    assert session.sets == [(True, False), (True, False)]
    stamps.clear()
    await _spin()
    assert session.sets[-1] == (False, False)  # the change to off is sent once
    channel.closed.set()
    await task


async def test_view_goes_to_frames_and_thumb_to_room(env):
    _, _, publisher = env
    channel = FakeChannel()
    task = await _start(FakeSession(), channel)
    channel.frame("view", 7)
    channel.frame("thumb", 8)
    await _spin()
    assert [(c[1]["payload"]["tier"], c[2]) for c in publisher.calls] == [("view", True), ("thumb", False)]
    event = publisher.calls[0][1]
    assert event["type"] == "room.browser.frame" and event["roomId"] == ROOM
    assert event["payload"] == {
        "roomId": ROOM, "runId": "r1", "agentId": "a1", "seq": 7, "jpegBase64": "J7", "w": 800, "h": 600,
        "tier": "view",
    }  # fmt: skip
    channel.closed.set()
    await task


async def test_newer_frame_overwrites_before_publish(env):
    _, _, publisher = env
    channel = FakeChannel()
    publisher.gate = asyncio.Event()
    task = await _start(FakeSession(), channel)
    channel.frame("view", 1)
    await _spin()
    channel.frame("view", 2)  # lands while seq 1 is still publishing
    channel.frame("view", 3)
    publisher.gate.set()
    await _spin()
    assert [c[1]["payload"]["seq"] for c in publisher.calls] == [1, 3]
    channel.closed.set()
    await task


async def test_publish_error_is_skipped(env, caplog):
    _, _, publisher = env
    publisher.publish_room = AsyncMock(side_effect=[RuntimeError("secret-frame"), True])
    channel = FakeChannel()
    task = await _start(FakeSession(), channel)
    channel.frame("view", 1)
    await _spin()
    channel.frame("view", 2)
    await _spin()
    assert publisher.publish_room.await_count == 2
    assert "RuntimeError" in caplog.text and "secret-frame" not in caplog.text
    channel.closed.set()
    await task


async def test_input_routes_to_the_live_run_and_unregisters_after_close(env):
    session, channel = FakeSession(), FakeChannel()
    task = await _start(session, channel)
    await mac_live.input(OWNER, "r1", [{"type": "text", "text": "hi"}])
    assert session.inputs == [[{"type": "text", "text": "hi"}]]
    with pytest.raises(LookupError):
        await mac_live.input(OWNER, "other", [])
    channel.closed.set()
    await task
    assert mac_live._live == {}
    with pytest.raises(LookupError):
        await mac_live.input(OWNER, "r1", [])


async def test_cast_set_failure_ends_quietly(env):
    now, stamps, _ = env
    session, channel = FakeSession(), FakeChannel()
    session.set_error = CdpClosed("CLOSED")
    task = await _start(session, channel)
    stamps["mac_view_at"] = now["t"]
    await _spin()
    assert task.done() and task.exception() is None
    assert mac_live._live == {}
