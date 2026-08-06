import json
import pytest
from core.gateway.connection_pool import GatewayConnection, _is_session_run_active


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
    conn._push_tasks = set()
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
    conn._push_tasks = set()
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
    conn._push_tasks = set()
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
async def test_mixed_case_session_key_clears_on_lowercased_terminal_event(monkeypatch):
    """Regression: ``build_chat_session_key`` embeds the raw (mixed-case)
    Clerk user_id, so the dispatcher's ``mark_run_active`` stores the
    active-run entry under a MIXED-CASE session_key. OpenClaw lowercases the
    ENTIRE session key on every event it emits (upstream
    session-key-utils.ts), so the chat-terminal branch in ``_handle_message``
    clears using a LOWERCASED key. If the store and the clear don't normalize
    the same way, the entry never clears — ``has_active_runs`` stays
    stale-True forever, and ``_grace_close`` permanently skips the
    trial-stop arm for any owner who has ever chatted (scale-to-zero dead)."""
    conn = GatewayConnection.__new__(GatewayConnection)
    # Realistic Clerk user id: "user_" prefix + base58, which is
    # near-guaranteed to contain uppercase characters.
    mixed_case_user_id = "user_2abcDEFghiJKLmnoPQR"
    conn.user_id = mixed_case_user_id
    conn.owner_id = mixed_case_user_id
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._push_tasks = set()
    # Run-completion observer registry (INV-5 bootstrap dispatcher). Not
    # exercised by this test, but _handle_message's TRUE-terminal branch
    # always pops from it now.
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()

    async def fake_forward(message, target_member_id=None):
        return None

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    # Dispatcher side: build_chat_session_key(agent_id, user_id, None) embeds
    # the raw mixed-case Clerk user_id.
    mixed_case_session_key = f"agent:agentA:{mixed_case_user_id}"
    conn.mark_run_active(mixed_case_session_key, agent_id="agentA")
    assert conn.has_active_runs is True

    # Event side: OpenClaw lowercases the whole sessionKey before it ever
    # reaches us.
    terminal_event = {
        "type": "event",
        "event": "chat",
        "payload": {
            "sessionKey": mixed_case_session_key.lower(),
            "state": "final",
            "runId": "run-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        },
    }
    await conn._handle_message(terminal_event)

    assert conn._active_runs == {}, "the active-run entry must clear despite the casing mismatch"
    assert conn.has_active_runs is False


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
    conn._push_tasks = set()
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


@pytest.mark.parametrize(
    "payload,expected",
    [
        # Ported 1:1 from OpenClaw's own ui/src/lib/session-run-state.test.ts —
        # this backend is just another one of its clients and must agree.
        ({"status": "running", "hasActiveRun": False}, False),
        ({"status": "running", "hasActiveRun": True}, True),
        # A terminal status WINS over a stale hasActiveRun:true. This is the
        # stuck "Working…" pill: the container's in-memory run registry leaks an
        # entry, but the durable per-turn status says the run ended.
        ({"status": "done", "hasActiveRun": True}, False),
        ({"status": "failed", "hasActiveRun": True}, False),
        ({"status": "killed", "hasActiveRun": True}, False),
        ({"status": "timeout", "hasActiveRun": True}, False),
        # No status (fresh/unknown session row) -> fall back to the flag.
        ({"hasActiveRun": True}, True),
        ({"hasActiveRun": False}, False),
        # Neither field: buildGatewaySessionSnapshot returns {} when there is no
        # session row, so absence must read as inactive, not as a crash.
        ({}, False),
        ({"status": "running"}, True),
        # Defensive: a non-bool hasActiveRun must not be coerced — fall through
        # to status rather than treating a truthy string as an active run.
        ({"status": "done", "hasActiveRun": "yes"}, False),
        ({"hasActiveRun": "yes"}, False),
    ],
)
def test_is_session_run_active_matches_upstream_rule(payload, expected):
    assert _is_session_run_active(payload) is expected


@pytest.mark.asyncio
async def test_sessions_changed_clears_pill_on_terminal_status_despite_stale_flag(monkeypatch):
    """Regression: the chat status pill stranded on "Working…" after a finished turn.

    A container under load leaks an entry in one of the three registries behind
    ``hasActiveRun``, so it keeps reporting true long after the run ended. The
    same payload carries the durable ``status``, which is terminal. Reading only
    the flag re-lit the pill with no closing false ever arriving.
    """
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._push_tasks = set()
    forwarded: list[dict] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append(message)

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    def changed(has_active_run, status):
        return {
            "type": "event",
            "event": "sessions.changed",
            "payload": {
                "sessionKey": "agent:agentA:user_1",
                "hasActiveRun": has_active_run,
                "status": status,
            },
        }

    # Run starts: status flips to "running" at run start, so the bar lights.
    await conn._handle_message(changed(True, "running"))
    # Run ends. The registry has leaked, so hasActiveRun is STILL true — but the
    # turn committed a terminal status alongside it.
    await conn._handle_message(changed(True, "done"))
    # Later stale re-broadcast of the same leaked flag: already deduped to
    # inactive, so nothing more is forwarded and the pill cannot re-light.
    await conn._handle_message(changed(True, "done"))

    run_active = [m for m in forwarded if m["type"] == "run_active"]
    assert run_active == [
        {"type": "run_active", "active": True, "agent_id": "agentA"},
        {"type": "run_active", "active": False, "agent_id": "agentA"},
    ]
