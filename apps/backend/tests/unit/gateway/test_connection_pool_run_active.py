import json
import time

import pytest

from core.gateway.connection_pool import GatewayConnection

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


def test_clear_active_run_retains_authorizing_session_for_terminal_idempotency():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {
        SESSION_KEY.lower(): {
            "started_at": time.monotonic(),
            "last_event_at": time.monotonic(),
            "agent_id": AGENT_ID,
            "awaiting_approval": False,
        }
    }
    conn._terminal_run_ids = {}
    conn._touch_run(SESSION_KEY, "run-1")

    conn._clear_active_run(SESSION_KEY, "run-1")

    assert conn._active_runs == {}
    assert conn._terminal_run_ids == {"run-1": SESSION_KEY}


@pytest.mark.asyncio
async def test_publish_user_event_org_owner_without_member_fans_out_to_all_users(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    conn.owner_id = "org_abc123"
    calls = []

    async def fake_publish_owner_users(owner_id, event):
        calls.append((owner_id, event))

    monkeypatch.setattr("core.services.live_events.publish_owner_users", fake_publish_owner_users)

    await conn._publish_user_event("runtime.resource.invalidated", {"resource": "agents"})

    assert len(calls) == 1
    assert calls[0][0] == "org_abc123"
    assert calls[0][1]["type"] == "runtime.resource.invalidated"


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

    await conn._publish_user_event("runtime.resource.invalidated", {"resource": "agents"})

    assert single_calls == [("user_abc123", "user_abc123", single_calls[0][2])]
    assert fanout_calls == []
