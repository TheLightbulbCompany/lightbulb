"""A hosted assistant (ChatGPT, Claude.ai, Grok...) reaches the lightbulb network THROUGH the
workspace member, as "<Client> via <Person>", with the engine's own tools: find_sessions,
send_message(session_id, request_id, reply_to), check_inbox and message_receipt. The backend
holds no network key and dials no Mac."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.services import hosted_mcp, network_member, network_route
from core.services.messaging import ApiProblem
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

OWNER, USER = "org_a", "user_ann"
PERSON = network_member.app_person(USER)
STARTING = "RUNTIME_UNAVAILABLE: the workspace is starting; retry with the same request_id"
ADEN = "ab12cd34/aden-mac"
OWN = f"w1w1w1w1/claude-code-{PERSON}"


class Engine:
    """The member's engine behind ``network.app``, in its real shapes (src/app_send.rs):
    ``app.send`` dedupes on request_id; ``app.inbox {peek}`` leaves mail until ``app.ack``."""

    def __init__(self):
        self.calls: list[dict] = []
        self.sent: dict[str, str] = {}
        self.mail: list[dict] = []
        self.lose_next_answer = False
        self.refuse_next: list[Exception] = []
        self.no_ack = False
        self.hang: asyncio.Event | None = None
        self.on_inbox = None

    async def __call__(self, owner, method, params):
        assert (owner, method) == (OWNER, "network.app")
        self.calls.append(params)
        if self.refuse_next:
            raise self.refuse_next.pop(0)
        op = params["op"]
        if self.hang is not None:
            await self.hang.wait()
        if op == "send":
            self.sent.setdefault(params["request_id"], params["text"])
            if self.lose_next_answer:
                self.lose_next_answer = False
                raise asyncio.TimeoutError("network.app timed out")
            return {"request_id": params["request_id"], "state": "delivered", "id": "m-sent"}
        if op == "receipt":
            rid = params["request_id"]
            return {"request_id": rid, "state": "delivered" if rid in self.sent else "unknown"}
        if op == "inbox":
            assert params.get("peek") is True  # a take would lose mail an answer never carried
            if self.on_inbox:
                self.on_inbox()
            return {"records": list(self.mail)}
        if op == "ack":
            if self.no_ack:  # an engine before app.ack: the bridge refuses the op
                raise RuntimeError(json.dumps({"code": "INVALID_REQUEST", "message": "op must be ..."}))
            before = len(self.mail)
            self.mail = [m for m in self.mail if m["id"] not in params["ids"]]
            return {"acked": before - len(self.mail)}
        if op == "unread":
            return {"unread": len(self.mail)}
        assert op == "directory"
        return {
            "sessions": [
                {"session_id": ADEN, "title": "Aden's Mac", "handle": "claude-5", "agent": "claude"},
                {"session_id": OWN, "title": "Claude Code via Ann", "agent": "claude-code"},
            ],
            "people": [],
        }

    def ops(self, *skip: str) -> list[str]:
        return [c["op"] for c in self.calls if c["op"] not in skip]


@pytest.fixture
def member(table, monkeypatch):
    """A warm workspace that is on the network, called by Ann's Claude Code."""
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(network_member.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr("core.services.hermes_profiles.put_owner_object", lambda k, b: objects.__setitem__(k, b))
    monkeypatch.setattr("core.services.hermes_profiles.get_owner_object", lambda k: objects.get(k))
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.repositories.network_keys_repo.keys_for_users", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ann Lee"))
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=[USER]))
    objects[network_member.sessions_key(OWNER)] = json.dumps(
        {"workspace": "Acme", "sessions": [], "joined": True}
    ).encode()
    events: list[str] = []
    engine = Engine()

    async def rpc(owner, method, params, **_kwargs):
        if method == "network.sessions":  # the warm sidecar is told sessions.json changed
            events.append("publish")
            return {}
        events.append(params["op"])
        return await engine(owner, method, params)

    monkeypatch.setattr(network_member.container_rpc, "call", rpc)
    warm = AsyncMock(return_value=True)
    monkeypatch.setattr(network_member, "_session_is_warm", warm)
    token = SimpleNamespace(
        client_id="https://claude.ai/oauth/claude-code-client-metadata",
        scopes=[hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        subject=USER,
        claims={"org_id": OWNER},
    )
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: token)
    monkeypatch.setattr(hosted_mcp, "sessions_for", AsyncMock(return_value={"sessions": [], "total": 0}))
    monkeypatch.setattr(hosted_mcp.webhook_agents, "targets", AsyncMock(return_value=[]))
    return SimpleNamespace(engine=engine, events=events, objects=objects, table=table, token=token, warm=warm)


def _app_entries(objects):
    doc = json.loads(objects[network_member.sessions_key(OWNER)])
    return [s for s in doc["sessions"] if "agentId" not in s]


def _pins(table):
    return [i for i in table.scan()["Items"] if str(i["sk"]).startswith("MCPSEND#")]


def _record(rid, text="hello"):
    return {"id": f"m-{rid}", "request_id": rid, "from": ADEN, "text": text, "framed": text}


@pytest.mark.asyncio
async def test_a_send_to_a_network_session_id_declares_the_app_session_then_sends(member):
    out = await hosted_mcp.send_message(text="hi Aden", request_id="req.1", session_id=ADEN)

    assert _app_entries(member.objects) == [
        {
            "name": f"claude-code-{PERSON}",
            "agent": "claude-code",
            "title": "Claude Code via Ann",
            "app": "claude-code",
            "person": PERSON,
        }
    ]
    assert member.events[:2] == ["publish", "send"]
    assert member.engine.calls[0] == {
        "op": "send",
        "app": "claude-code",
        "person": PERSON,
        "to": ADEN,
        "text": "hi Aden",
        "request_id": "req.1",
    }
    assert out == {
        "destination": f"network:{ADEN}",
        "requestId": "req.1",
        "request_id": "req.1",
        "state": "delivered",
        "id": "m-sent",
    }


@pytest.mark.asyncio
async def test_reply_to_alone_answers_whoever_sent_that_message(member):
    out = await hosted_mcp.send_message(text="pong", request_id="r2", reply_to="q1")
    assert member.engine.calls[0] == {
        "op": "send",
        "app": "claude-code",
        "person": PERSON,
        "to": None,
        "text": "pong",
        "request_id": "r2",
        "reply_to": "q1",
    }
    assert out["destination"] == "network:reply:q1"
    await hosted_mcp.send_message(text="pong", request_id="r3", session_id=ADEN, reply_to="q1")
    assert member.engine.calls[-2]["to"] == ADEN and member.engine.calls[-2]["reply_to"] == "q1"


@pytest.mark.asyncio
async def test_a_retry_with_the_same_request_id_sends_once(member):
    member.engine.lose_next_answer = True
    with pytest.raises(ToolError) as lost:
        await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    assert str(lost.value) == STARTING

    out = await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    assert out["state"] == "delivered"
    assert list(member.engine.sent) == ["r1"]
    assert member.events.count("publish") == 1  # the app session was new only the first time
    assert len(_pins(member.table)) == 1
    receipt = await hosted_mcp.message_receipt("r1")
    assert member.engine.calls[-2] == {"op": "receipt", "app": "claude-code", "person": PERSON, "request_id": "r1"}
    assert receipt == {"destination": f"network:{ADEN}", "requestId": "r1", "request_id": "r1", "state": "delivered"}


@pytest.mark.asyncio
async def test_request_ids_follow_the_destination(member):
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.send_message(text="hi", request_id="not ok!", session_id=ADEN)
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.send_message(text="hi", request_id="x" * 129, session_id=ADEN)
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.send_message(text="hi", request_id="r1\n", session_id=ADEN)
    with pytest.raises(ToolError, match="UUID"):  # a room send still takes a UUID
        await hosted_mcp.send_message(text="hi", request_id="r1", room_id=str(uuid.uuid4()))
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.message_receipt("not ok!")
    assert member.engine.ops("unread") == []


@pytest.mark.asyncio
async def test_a_uuid_session_id_is_a_shared_lightbulb_session_not_the_network(member, monkeypatch):
    refused = AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_NOT_FOUND", "no such session"))
    monkeypatch.setattr(hosted_mcp.local_sessions, "authorize_session", refused)
    with pytest.raises(ToolError, match="LOCAL_SESSION_NOT_FOUND"):
        await hosted_mcp.send_message(text="hi", request_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
    assert member.engine.ops("unread") == []
    with pytest.raises(ApiProblem):  # reply_to is a network reply: never beside a shared session
        await hosted_mcp.send_message(text="hi", request_id="r1", session_id=str(uuid.uuid4()), reply_to="q1")


@pytest.mark.asyncio
async def test_exactly_one_destination_and_no_cross_destination_reuse(member):
    with pytest.raises(ApiProblem) as both:
        await hosted_mcp.send_message(text="hi", request_id="r1", room_id=str(uuid.uuid4()), session_id=ADEN)
    assert both.value.error.code == "INVALID_REQUEST"
    await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
        await hosted_mcp.send_message(text="hi", request_id="r1", session_id="ab12cd34/someone-else")
    assert member.engine.ops("unread") == ["send"]


@pytest.mark.asyncio
async def test_check_inbox_peeks_builds_the_answer_then_acks(member):
    member.engine.mail = [_record("q1"), _record("q2", "again")]
    out = await hosted_mcp.check_inbox()
    assert [m["text"] for m in out["messages"]] == ["hello", "again"]
    assert member.engine.ops("unread") == ["inbox", "ack"]
    assert member.engine.calls[1] == {"op": "ack", "app": "claude-code", "person": PERSON, "ids": ["m-q1", "m-q2"]}
    assert "unread" not in out
    assert (await hosted_mcp.check_inbox())["messages"] == []
    assert _pins(member.table) == []


@pytest.mark.asyncio
async def test_a_lost_ack_repeats_mail_and_never_loses_it(member):
    member.engine.mail = [_record("q1")]
    member.engine.no_ack = True
    first = await hosted_mcp.check_inbox()
    second = await hosted_mcp.check_inbox()
    assert first["messages"] == second["messages"] == [_record("q1")]
    member.engine.refuse_next = [RuntimeError(json.dumps({"code": "UNAVAILABLE"}))]
    with pytest.raises(ToolError, match="RUNTIME_UNAVAILABLE"):  # the peek failed: nothing was taken
        await hosted_mcp.check_inbox()
    member.engine.no_ack = False
    assert (await hosted_mcp.check_inbox())["messages"] == [_record("q1")]
    assert (await hosted_mcp.check_inbox())["messages"] == []


@pytest.mark.asyncio
async def test_find_sessions_lists_the_network_sessions_in_the_engines_shape(member):
    out = await hosted_mcp.find_sessions(query="aden")
    assert member.engine.calls[0] == {"op": "directory", "app": "claude-code", "person": PERSON, "query": "aden"}
    assert out["sessions"] == [
        {"session_id": ADEN, "title": "Aden's Mac", "name": "claude-5", "agent": "claude", "is_this_session": False},
        {"session_id": OWN, "title": "Claude Code via Ann", "agent": "claude-code", "is_this_session": True},
    ]
    assert out["total"] == 2 and out["agents"] == [] and "network" not in out


@pytest.mark.asyncio
async def test_find_sessions_without_a_network_lists_the_rest(member):
    member.objects.clear()  # not a member, and no computer of Ann's online either
    out = await hosted_mcp.find_sessions()
    assert out == {"sessions": [], "total": 0, "agents": [], "network": f"NETWORK_NO_ROUTE: {network_route.NO_ROUTE}"}


@pytest.mark.asyncio
async def test_a_cold_workspace_answers_starting_at_once_and_queues_one_wake(member):
    await network_member.write_sessions(OWNER)  # the doorbell row a member's sessions.json carries
    member.warm.return_value = False
    for _ in range(2):
        with pytest.raises(ToolError) as cold:
            await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
        assert str(cold.value) == STARTING
    assert member.engine.calls == []  # no invoke waited on a cold start
    wakes = [i for i in member.table.scan()["Items"] if str(i["sk"]).startswith("WORK#NETWAKE#")]
    assert [w["payload"] for w in wakes] == [{"kind": "network_wake", "ownerId": OWNER}]  # the window holds one
    out = await hosted_mcp.find_sessions()
    assert out["network"] == hosted_mcp._PARKED and out["sessions"] == []
    assert "request_id" not in out["network"]
    assert len([i for i in member.table.scan()["Items"] if str(i["sk"]).startswith("WORK#NETWAKE#")]) == 1


@pytest.mark.asyncio
async def test_a_hosted_caller_gets_the_unread_line_only_while_warm(member):
    await hosted_mcp.check_inbox()  # declares the app session
    member.engine.mail = [_record("q1")]
    out = await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    assert out["unread"] == "1 unread message for this session; call check_inbox to read them."
    member.warm.return_value = False
    member.engine.calls.clear()
    out = await hosted_mcp.find_sessions()
    assert "unread" not in out and member.engine.calls == []  # a note never wakes the workspace


@pytest.mark.asyncio
async def test_the_bridges_unavailable_answer_is_the_starting_error(member):
    off = {"code": "UNAVAILABLE", "message": "network.app is not available on the Hermes runtime"}
    member.engine.refuse_next = [RuntimeError(json.dumps(off)), ApiProblem("RUNTIME_UNAVAILABLE", "starting")]
    for rid in ("a", "b"):
        with pytest.raises(ToolError) as err:
            await hosted_mcp.send_message(text="hi", request_id=rid, session_id=ADEN)
        assert str(err.value) == STARTING


@pytest.mark.asyncio
async def test_a_sidecar_that_never_learned_the_entry_is_told_again(member):
    await hosted_mcp.check_inbox()  # the entry is in sessions.json now
    member.events.clear()
    refused = {"code": "refused", "message": "no such app session", "reason": "not_declared"}
    member.engine.refuse_next = [RuntimeError(json.dumps(refused))]
    out = await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    assert out["state"] == "delivered"
    assert member.events[:3] == ["send", "publish", "send"]
    assert len(_app_entries(member.objects)) == 1


@pytest.mark.asyncio
async def test_an_unknown_name_is_never_published_and_heals_on_redeclare(member, monkeypatch):
    lookup = AsyncMock(return_value=USER)  # what the lookup answers when Clerk cannot say
    monkeypatch.setattr("core.services.messaging.human_display_name", lookup)
    await hosted_mcp.check_inbox()
    [entry] = _app_entries(member.objects)
    assert entry["title"] == "Claude Code via a teammate" and USER not in json.dumps(entry)

    lookup.return_value = "Ann Lee"
    member.engine.refuse_next = [RuntimeError(json.dumps({"code": "refused", "reason": "not_declared"}))]
    await hosted_mcp.check_inbox()
    assert [e["title"] for e in _app_entries(member.objects)] == ["Claude Code via Ann"]


@pytest.mark.asyncio
async def test_the_receipt_reads_the_app_session_that_sent_it(member):
    await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    member.token.client_id = "https://chatgpt.com/oauth/codex/client.json"
    with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
        await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    receipt = await hosted_mcp.message_receipt("r1")
    assert [c for c in member.engine.calls if c["op"] == "receipt"][-1]["app"] == "claude-code"
    assert receipt["state"] == "delivered"
    assert list(member.engine.sent) == ["r1"]


@pytest.mark.asyncio
async def test_the_assistant_apps_send_as_claude_and_chatgpt(member):
    member.token.client_id = "https://claude.ai/oauth/mcp-client-metadata"
    await hosted_mcp.send_message(text="hi", request_id="c1", session_id=ADEN)
    member.token.client_id = "https://chatgpt.com/oauth/abc123/client.json"
    await hosted_mcp.send_message(text="hi", request_id="g1", session_id=ADEN)
    assert [(e["app"], e["title"]) for e in _app_entries(member.objects)] == [
        ("claude", "Claude via Ann"),
        ("chatgpt", "ChatGPT via Ann"),
    ]


@pytest.mark.asyncio
async def test_a_hung_member_answers_starting_within_the_budget(member, monkeypatch):
    monkeypatch.setattr(hosted_mcp, "HOSTED_BUDGET_S", 0.3)
    member.engine.hang = asyncio.Event()  # a stale warm estimate: the invoke would wait 90 s
    with pytest.raises(ToolError) as err:
        await asyncio.wait_for(hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN), 5)
    assert str(err.value) == STARTING
    with pytest.raises(ToolError):
        await asyncio.wait_for(hosted_mcp.check_inbox(), 5)
    assert "ack" not in member.engine.ops()  # no ack after a lost peek


@pytest.mark.asyncio
async def test_a_late_peek_is_not_acked_so_the_mail_repeats(member, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hosted_mcp, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0))
    member.engine.mail = [_record("q1")]
    member.engine.on_inbox = lambda: clock.__setitem__(0, clock[0] + 23)  # past API Gateway's cut
    assert (await hosted_mcp.check_inbox())["messages"] == [_record("q1")]
    assert "ack" not in member.engine.ops()
    member.engine.on_inbox = None
    assert (await hosted_mcp.check_inbox())["messages"] == [_record("q1")]
    assert member.engine.ops().count("ack") == 1


@pytest.mark.asyncio
async def test_check_inbox_waits_by_polling_the_count(member, monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            member.engine.mail = [_record("q1")]

    monkeypatch.setattr(hosted_mcp, "_sleep", sleep)
    out = await hosted_mcp.check_inbox(wait_secs=600)
    assert out["messages"] == [_record("q1")]
    assert member.engine.ops()[:5] == ["unread", "unread", "unread", "inbox", "ack"]
    assert slept == [1.0, 1.5]
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.check_inbox(wait_secs=601)


@pytest.mark.asyncio
async def test_check_inbox_wait_ends_inside_the_budget(member, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(hosted_mcp, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0))

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(hosted_mcp, "_sleep", sleep)
    out = await hosted_mcp.check_inbox(wait_secs=60)
    assert out["messages"] == []
    assert clock[0] <= hosted_mcp.HOSTED_BUDGET_S - hosted_mcp._RESERVE_S  # room left for peek and ack


@pytest.mark.asyncio
async def test_the_unread_line_rides_only_the_network_tools(member, monkeypatch):
    monkeypatch.setattr(hosted_mcp, "rooms_for", AsyncMock(return_value={"rooms": []}))
    assert "unread" not in await hosted_mcp.find_rooms()  # before any network call: not declared
    await hosted_mcp.check_inbox()
    member.engine.mail = [_record("q1")]
    member.engine.calls.clear()
    assert "unread" not in await hosted_mcp.find_rooms()
    assert member.engine.calls == []  # a room read never touches the runtime
    assert (await hosted_mcp.message_receipt("nope"))["unread"].startswith("1 unread message")


@pytest.mark.asyncio
async def test_an_undeclared_app_session_is_never_asked_for_its_count(member):
    member.engine.mail = [_record("q1")]
    await hosted_mcp.message_receipt("nope")
    assert member.engine.calls == []


@pytest.mark.asyncio
async def test_no_field_overrides_the_app_session(member):
    await network_member.app_call(
        SimpleNamespace(user_id=USER, org_id=OWNER),
        "claude-code",
        "Claude Code",
        "inbox",
        app="other",
        person="deadbeef",
        peek=True,
    )
    assert member.engine.calls[-1] == {"op": "inbox", "app": "claude-code", "person": PERSON, "peek": True}


@pytest.mark.asyncio
async def test_a_uuid_shaped_network_request_id_reads_its_own_receipt(member):
    rid = str(uuid.uuid4()).upper()
    await hosted_mcp.send_message(text="hi", request_id=rid, session_id=ADEN)
    assert (await hosted_mcp.message_receipt(rid))["state"] == "delivered"
