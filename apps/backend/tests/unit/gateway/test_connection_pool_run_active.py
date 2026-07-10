import json
import pytest
from core.gateway.connection_pool import GatewayConnection


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_subscribe_session_events_sends_sessions_subscribe():
    conn = GatewayConnection.__new__(GatewayConnection)  # skip __init__ networking
    conn._ws = FakeWS()
    await conn._subscribe_session_events()
    methods = [m.get("method") for m in conn._ws.sent]
    assert "sessions.subscribe" in methods
    frame = next(m for m in conn._ws.sent if m.get("method") == "sessions.subscribe")
    assert frame["type"] == "req"
    assert frame["params"] == {}
    assert frame.get("id")


@pytest.mark.asyncio
async def test_sessions_changed_forwards_run_active_for_webchat(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[tuple[dict, str | None]] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append((message, target_member_id))

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    evt = {
        "type": "event",
        "event": "sessions.changed",
        "payload": {"sessionKey": "agent:agentA:user_1", "hasActiveRun": True, "status": "running"},
    }
    await conn._handle_message(evt)

    run_active = [m for (m, _t) in forwarded if m.get("type") == "run_active"]
    assert run_active == [{"type": "run_active", "active": True, "agent_id": "agentA"}]


@pytest.mark.asyncio
async def test_sessions_changed_dedupes_and_scopes(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[dict] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append(message)

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    def changed(key, active):
        return {"type": "event", "event": "sessions.changed", "payload": {"sessionKey": key, "hasActiveRun": active}}

    # webchat: active -> forwarded once; duplicate active -> not re-forwarded
    await conn._handle_message(changed("agent:agentA:user_1", True))
    await conn._handle_message(changed("agent:agentA:user_1", True))
    # a cron session must NEVER forward run_active
    await conn._handle_message(changed("agent:agentA:cron:job1", True))
    # transition to inactive -> forwarded
    await conn._handle_message(changed("agent:agentA:user_1", False))

    run_active = [m for m in forwarded if m.get("type") == "run_active"]
    assert run_active == [
        {"type": "run_active", "active": True, "agent_id": "agentA"},
        {"type": "run_active", "active": False, "agent_id": "agentA"},
    ]


@pytest.mark.asyncio
async def test_sessions_changed_ignores_unclassified_key(monkeypatch):
    # A truthy but malformed/unclassified sessionKey parses to source "" — it is
    # NOT one of the known non-webchat sources, so a deny-list would let it fall
    # through and broadcast (target_member=None) to every one of the owner's chat
    # connections. The allow-list gate (source == "webchat") must drop it.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[tuple[dict, str | None]] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append((message, target_member_id))

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    for bad_key in ("garbage", "not-an-agent-key", "x:y"):
        await conn._handle_message(
            {"type": "event", "event": "sessions.changed", "payload": {"sessionKey": bad_key, "hasActiveRun": True}}
        )

    assert [m for (m, _t) in forwarded if m.get("type") == "run_active"] == []


@pytest.mark.asyncio
async def test_reset_run_state_dedup_relights_on_reconnect(monkeypatch):
    # A tab that (re)connects mid-run clears its own containerRunActive, so the
    # backend must re-announce the still-active run. Dedup would otherwise
    # suppress the ongoing hasActiveRun:true; reset_run_state_dedup() (called on
    # frontend connect) makes the next sessions.changed a fresh transition again.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[dict] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append(message)

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    def changed(active):
        return {
            "type": "event",
            "event": "sessions.changed",
            "payload": {"sessionKey": "agent:agentA:user_1", "hasActiveRun": active},
        }

    await conn._handle_message(changed(True))  # forwarded (transition)
    await conn._handle_message(changed(True))  # deduped
    assert len([m for m in forwarded if m["type"] == "run_active"]) == 1

    conn.reset_run_state_dedup()  # simulate a frontend (re)connect

    await conn._handle_message(changed(True))  # re-forwarded for the new tab
    run_active = [m for m in forwarded if m["type"] == "run_active"]
    assert run_active == [
        {"type": "run_active", "active": True, "agent_id": "agentA"},
        {"type": "run_active", "active": True, "agent_id": "agentA"},
    ]
