"""Recall's per-person audio into LiveKit: one seat per Meet person, never the bot, at most 16."""

import base64
import json
from types import SimpleNamespace

import jwt
import pytest

from core.services import meet_bridge

ROOM = "11111111-1111-4111-8111-111111111111"
BOT = "Acme" + meet_bridge.meeting_bots._BOT_SUFFIX


class FakeRoom:
    fail_for: set[str] = set()

    def __init__(self, log):
        self.log, self.identity, self.published = log, None, []
        self.local_participant = SimpleNamespace(publish_track=self._publish)

    async def connect(self, url, token, options):
        claims = jwt.decode(token, "s" * 32, algorithms=["HS256"])
        if claims["name"] in FakeRoom.fail_for:
            raise RuntimeError("lk down")
        self.identity, self.name, self.claims, self.options = claims["sub"], claims["name"], claims, options
        self.log.rooms.append(self)

    async def _publish(self, track, options):
        self.published.append((track, options))

    async def disconnect(self):
        self.log.disconnected.append(self.identity)


class FakeSource:
    def __init__(self, log, rate, channels):
        self.log, self.rate, self.channels = log, rate, channels

    async def capture_frame(self, frame):
        self.log.frames.append((self, frame))

    async def aclose(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    log = SimpleNamespace(rooms=[], disconnected=[], frames=[], metrics=[])
    FakeRoom.fail_for = set()
    rtc = SimpleNamespace(
        Room=lambda: FakeRoom(log),
        RoomOptions=lambda auto_subscribe: SimpleNamespace(auto_subscribe=auto_subscribe),
        AudioSource=lambda rate, channels: FakeSource(log, rate, channels),
        AudioFrame=lambda data, rate, channels, samples: SimpleNamespace(
            data=data, rate=rate, channels=channels, samples=samples
        ),
        LocalAudioTrack=SimpleNamespace(create_audio_track=lambda name, source: SimpleNamespace(name=name)),
        TrackPublishOptions=lambda source: SimpleNamespace(source=source),
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE="mic-source"),
    )
    monkeypatch.setattr(meet_bridge, "rtc", rtc)
    monkeypatch.setattr(meet_bridge.settings, "LIVEKIT_URL", "wss://lk.test")
    monkeypatch.setattr(meet_bridge.settings, "LIVEKIT_API_KEY", "k")
    monkeypatch.setattr(meet_bridge.settings, "LIVEKIT_API_SECRET", "s" * 32)
    monkeypatch.setattr(meet_bridge.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(meet_bridge, "put_metric", lambda name, value=1.0, **kw: log.metrics.append((name, value, kw)))
    return log


def _event(kind, pid, name, pcm=None):
    data = {"participant": {"id": pid, "name": name, "is_host": False, "platform": "desktop"}}
    if pcm is not None:
        data |= {"buffer": base64.b64encode(pcm).decode(), "timestamp": {"relative": 1.0, "absolute": "x"}}
    return json.dumps({"event": kind, "data": {"data": data, "bot": {"id": "bot-1", "metadata": {}}}})


def _audio(pid, name, pcm):
    return _event("audio_separate_raw.data", pid, name, pcm)


async def test_join_seats_one_person_with_their_name(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_event("participant_events.join", 100, "Priya"))
    await bridge.feed(_event("participant_events.join", 100, "Priya"))  # a repeat seats no one new
    await bridge.drain()
    (room,) = fake.rooms
    assert room.identity == f"meet:{ROOM}:100" and room.name == "Priya"
    assert room.claims["video"]["room"] == f"meet-dev-{ROOM}"
    assert room.claims["video"]["canSubscribe"] is False and room.claims["video"]["canPublishSources"] == ["microphone"]
    assert room.options.auto_subscribe is False
    ((track, opts),) = room.published
    assert track.name == "mic" and opts.source == "mic-source"
    assert ("meeting.bridge.connected", 1.0, {}) in fake.metrics


async def test_audio_seats_lazily_and_reaches_the_source(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_audio(200, "", b"\x01\x00" * 160))
    await bridge.feed(_audio(200, "", b""))  # an unmuted silent person: nothing to capture
    (room,) = fake.rooms
    assert room.name == "Guest"
    ((source, frame),) = fake.frames
    assert (source.rate, source.channels) == (16000, 1)
    assert (frame.rate, frame.channels, frame.samples) == (16000, 1, 160) and frame.data == b"\x01\x00" * 160


async def test_long_names_are_cut(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_audio(1, "x" * 200, b"\x00\x00"))
    assert fake.rooms[0].name == "x" * 80


async def test_leave_disconnects(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_audio(100, "Priya", b"\x00\x00"))
    await bridge.feed(_event("participant_events.leave", 100, "Priya"))
    assert fake.disconnected == [f"meet:{ROOM}:100"]
    await bridge.close()
    assert fake.disconnected == [f"meet:{ROOM}:100"]  # already gone: not twice


async def test_the_bot_is_never_seated(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_event("participant_events.join", 1, BOT))
    await bridge.feed(_audio(1, BOT, b"\x00\x00"))
    await bridge.drain()
    assert fake.rooms == [] and fake.frames == []


async def test_seventeenth_person_is_ignored_once(fake):
    bridge = meet_bridge.MeetBridge(ROOM)
    for pid in range(17):
        await bridge.feed(_event("participant_events.join", pid, f"p{pid}"))
    await bridge.feed(_event("participant_events.join", 99, "late"))
    await bridge.drain()
    assert len(fake.rooms) == 16
    capped = ("meeting.bridge.error", 1.0, {"dimensions": {"why": "capped"}})
    assert [m for m in fake.metrics if m == capped] == [capped]  # once per meeting
    await bridge.close()
    assert len(fake.disconnected) == 16
    assert ("meeting.bridge.participants", 16, {}) in fake.metrics


async def test_a_full_room_seats_who_speaks_over_who_only_joined(fake):
    """Recall's cap is 16 concurrent speakers, not the first 16 to join a large Meet."""
    bridge = meet_bridge.MeetBridge(ROOM)
    for pid in range(16):
        await bridge.feed(_event("participant_events.join", pid, f"p{pid}"))
    await bridge.feed(_audio(0, "p0", b"\x00\x00"))  # p0 has spoken: not the one to lose a seat
    await bridge.feed(_audio(99, "late", b"\x00\x00"))
    assert fake.disconnected == [f"meet:{ROOM}:1"]  # the longest-silent seat made room
    assert fake.rooms[-1].name == "late" and len(fake.frames) == 2
    await bridge.feed(_audio(1, "p1", b"\x00\x00"))  # the one who lost it speaks: p2 (never heard) makes room
    assert fake.disconnected == [f"meet:{ROOM}:1", f"meet:{ROOM}:2"]
    assert len(fake.frames) == 3
    await bridge.close()


async def test_one_failed_seat_leaves_the_others_working(fake):
    FakeRoom.fail_for = {"Broken"}
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(_audio(1, "Broken", b"\x00\x00"))
    await bridge.feed(_audio(1, "Broken", b"\x00\x00"))
    await bridge.feed(_audio(2, "Dan", b"\x00\x00"))
    assert [r.name for r in fake.rooms] == ["Dan"] and len(fake.frames) == 1
    errors = [m for m in fake.metrics if m[0] == "meeting.bridge.error"]
    assert errors == [("meeting.bridge.error", 1.0, {"dimensions": {"why": "connect"}})]  # once, not per packet
    await bridge.close()
    assert [i for i in fake.disconnected if i] == [f"meet:{ROOM}:2"]  # the failed room was only cleaned up


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        "[]",
        json.dumps({"event": "transcript.data", "data": {}}),
        json.dumps({"event": "audio_separate_raw.data", "data": {"data": {"buffer": "!!", "participant": {}}}}),
        json.dumps({"event": "audio_separate_raw.data", "data": None}),
    ],
)
async def test_malformed_and_unknown_frames_are_ignored(fake, frame):
    bridge = meet_bridge.MeetBridge(ROOM)
    await bridge.feed(frame)
    await bridge.drain()
    assert fake.rooms == [] and fake.frames == []
