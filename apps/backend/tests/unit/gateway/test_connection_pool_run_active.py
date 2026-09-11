import asyncio
import json

import pytest

from core.gateway.connection_pool import GatewayConnection, _is_session_run_active

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_subscribe_session_events_sends_sessions_subscribe():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._ws = FakeWS()
    await conn._subscribe_session_events()
    frame = next(message for message in conn._ws.sent if message.get("method") == "sessions.subscribe")
    assert frame["type"] == "req"
    assert frame["params"] == {}
    assert frame.get("id")


@pytest.mark.asyncio
async def test_sessions_changed_publishes_native_run_activity_and_dedupes(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn._run_state = {}
    conn._terminal_run_ids = {}
    frames = []

    async def publish(_session_key, frame):
        frames.append(frame)

    monkeypatch.setattr(conn, "_publish_room_frame", publish)
    conn.mark_run_active(SESSION_KEY, agent_id=AGENT_ID)

    def changed(active, status):
        return {
            "type": "event",
            "event": "sessions.changed",
            "payload": {
                "sessionKey": SESSION_KEY,
                "runId": "run-1",
                "hasActiveRun": active,
                "status": status,
            },
        }

    await conn._handle_message(changed(True, "running"))
    await conn._handle_message(changed(True, "running"))
    await conn._handle_message(changed(True, "done"))

    assert frames == [
        {"type": "run_active", "active": True, "agent_id": AGENT_ID, "runId": "run-1"},
        {"type": "run_active", "active": False, "agent_id": AGENT_ID, "runId": "run-1"},
    ]


def test_clear_active_run_retains_authorizing_session_for_terminal_idempotency():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn._run_state = {}
    conn._terminal_run_ids = {}
    conn.mark_run_active(SESSION_KEY, agent_id=AGENT_ID)
    conn._touch_run(SESSION_KEY, "run-1")

    conn._clear_active_run(SESSION_KEY, "run-1")

    assert conn._active_runs == {}
    assert conn._terminal_run_ids == {"run-1": SESSION_KEY}


@pytest.mark.asyncio
async def test_timeout_cleanup_keeps_thread_routing_for_continuing_frames(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn._room_run_context = {}
    conn._run_state = {}
    conn._terminal_run_ids = {}
    conn._live_publish_lock = asyncio.Lock()
    conn.owner_id = "owner-1"
    published = []

    class FakePublisher:
        async def publish_room(self, _room_id, event):
            published.append(event)

    async def get_room(_room_id):
        return {"ownerId": "owner-1"}

    async def get_member(_room_id, _kind, _agent_id):
        return {"id": AGENT_ID}

    monkeypatch.setattr("core.repositories.messaging_repo.get_room", get_room)
    monkeypatch.setattr("core.repositories.messaging_repo.get_member", get_member)
    monkeypatch.setattr("core.services.live_events.get_live_event_publisher", lambda: FakePublisher())

    conn.mark_run_active(SESSION_KEY, agent_id=AGENT_ID, thread_root_message_id="thread-1")
    conn._clear_active_run(SESSION_KEY, preserve_room_context=True)
    assert conn.has_active_runs is False

    await conn._publish_room_frame(SESSION_KEY, {"type": "chunk", "content": "still running", "runId": "run-1"})

    assert published[0]["payload"]["threadRootMessageId"] == "thread-1"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "running", "hasActiveRun": False}, False),
        ({"status": "running", "hasActiveRun": True}, True),
        ({"status": "done", "hasActiveRun": True}, False),
        ({"status": "failed", "hasActiveRun": True}, False),
        ({"status": "killed", "hasActiveRun": True}, False),
        ({"status": "timeout", "hasActiveRun": True}, False),
        ({"hasActiveRun": True}, True),
        ({"hasActiveRun": False}, False),
        ({}, False),
        ({"status": "running"}, True),
        ({"status": "done", "hasActiveRun": "yes"}, False),
        ({"hasActiveRun": "yes"}, False),
    ],
)
def test_is_session_run_active_matches_upstream_rule(payload, expected):
    assert _is_session_run_active(payload) is expected


def test_mark_run_active_first_event_kind_wins_over_ambient_remark():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn.mark_run_active(SESSION_KEY, agent_id=AGENT_ID, event_kind="user_request")
    conn.mark_run_active(SESSION_KEY, agent_id=AGENT_ID, event_kind="room_event")

    assert conn._active_runs[SESSION_KEY.lower()]["event_kind"] == "user_request"


@pytest.mark.asyncio
async def test_publish_user_event_org_owner_without_member_fans_out_to_all_users(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn.owner_id = "org_abc123"
    calls = []

    async def fake_publish_owner_users(owner_id, event):
        calls.append((owner_id, event))

    monkeypatch.setattr("core.services.live_events.publish_owner_users", fake_publish_owner_users)

    await conn._publish_user_event("runtime.approval.requested", {"foo": "bar"})

    assert len(calls) == 1
    assert calls[0][0] == "org_abc123"
    assert calls[0][1]["type"] == "runtime.approval.requested"


@pytest.mark.asyncio
async def test_publish_user_event_user_owner_uses_single_user_path(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn.owner_id = "user_abc123"
    single_calls = []
    fanout_calls = []

    class FakePublisher:
        async def publish_user(self, owner_id, user_id, event):
            single_calls.append((owner_id, user_id, event))

    async def fake_publish_owner_users(owner_id, event):
        fanout_calls.append((owner_id, event))

    monkeypatch.setattr("core.services.live_events.get_live_event_publisher", lambda: FakePublisher())
    monkeypatch.setattr("core.services.live_events.publish_owner_users", fake_publish_owner_users)

    await conn._publish_user_event("runtime.approval.requested", {"foo": "bar"})

    assert single_calls == [("user_abc123", "user_abc123", single_calls[0][2])]
    assert fanout_calls == []


@pytest.mark.asyncio
async def test_subscribe_room_session_subscribes_once_and_survives_reconnect():
    """A channel-initiated run is `isControlUiVisible: false` upstream, so the
    gateway streams its agent/chat events only to a session-message subscriber.
    Without this RPC the room gets no run frames at all."""
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._ws = FakeWS()
    conn._room_session_subs = set()
    conn.user_id = "user_abc123"

    await conn.subscribe_room_session(SESSION_KEY)
    await conn.subscribe_room_session(SESSION_KEY)

    def subscribes(ws):
        return [m for m in ws.sent if m.get("method") == "sessions.messages.subscribe"]

    assert [m["params"] for m in subscribes(conn._ws)] == [{"key": SESSION_KEY}]
    assert subscribes(conn._ws)[0]["type"] == "req"
    assert subscribes(conn._ws)[0].get("id")

    # A reconnect replays the set onto the new socket.
    conn._ws = FakeWS()
    for key in list(conn._room_session_subs):
        await conn._send_room_session_subscribe(key)
    assert [m["params"] for m in subscribes(conn._ws)] == [{"key": SESSION_KEY}]
