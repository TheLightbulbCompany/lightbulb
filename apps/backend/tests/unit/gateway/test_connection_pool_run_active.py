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
