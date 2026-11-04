"""A hosted assistant (ChatGPT, Claude.ai, Grok...) reaches the lightbulb network THROUGH the
signed-in computer its connection is pinned to, as "<Client> via <Person>", with the engine's own
tools: find_sessions, send_message(session_id, request_id, reply_to), check_inbox and
message_receipt. The backend holds no network key."""

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
ADEN = "ab12cd34/aden-mac"
OWN = f"w1w1w1w1/claude-code-{PERSON}"
CODING = "ab12cd34/claude:conv1"  # a coding session: provider:conversation, never always-on


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
        self.results: dict[str, dict] = {}

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
            if rid in self.results:
                return {"request_id": rid, "state": "delivered", "result": self.results[rid]}
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
                {"session_id": ADEN, "title": "Aden's Mac", "handle": "claude-5", "agent": "claude", "online": True},
                {"session_id": OWN, "title": "Claude Code via Ann", "agent": "claude-code", "online": True},
                {"session_id": CODING, "title": "Aden's Claude", "agent": "claude", "online": True},
                {"session_id": f"ab12cd34/sleepy-{PERSON}", "title": "Sleepy", "agent": "claude", "online": False},
                {"session_id": "ab12cd34/terminal", "title": "Terminal", "agent": "terminal", "online": True},
            ],
            "people": [],
        }

    def ops(self, *skip: str) -> list[str]:
        return [c["op"] for c in self.calls if c["op"] not in skip]


@pytest.fixture
def member(table, monkeypatch):
    """Ann's Claude Code, pinned to her online computer, whose engine answers the app ops."""
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ann Lee"))
    engine = Engine()
    route = {"kind": "computer", "key": "kann", "name": "Ann's Mac"}
    monkeypatch.setattr(network_route, "route_for", AsyncMock(return_value=route))
    sent_to: list[dict] = []

    titles: list[str] = []

    async def link(row, op, params, timeout_s):  # the computer's link (network_route.call)
        sent_to.append(row)
        params = dict(params)
        if "title" in params:
            titles.append(params.pop("title"))  # rides every app op: it declares the session there
        return await engine(OWNER, "network.app", {**params, "op": op.removeprefix("app.")})

    rows = [{"key": "kann", "userId": USER, "generation": "g", "connectedAt": 1, "taskAddress": "x"}]
    monkeypatch.setattr(
        network_route, "_connections", lambda: SimpleNamespace(live_routes=AsyncMock(return_value=rows))
    )
    monkeypatch.setattr(network_route, "call", link)
    token = SimpleNamespace(
        client_id="https://claude.ai/oauth/claude-code-client-metadata",
        scopes=[hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        subject=USER,
        claims={"org_id": OWNER},
    )
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: token)
    return SimpleNamespace(engine=engine, table=table, token=token, rows=rows, sent_to=sent_to, titles=titles)


def _pins(table):
    return [i for i in table.scan()["Items"] if str(i["sk"]).startswith("MCPSEND#")]


def _record(rid, text="hello"):
    return {"id": f"m-{rid}", "request_id": rid, "from": ADEN, "text": text, "framed": text}


@pytest.mark.asyncio
async def test_a_send_to_a_network_session_id_goes_through_the_pinned_computer(member):
    out = await hosted_mcp.send_message(text="hi Aden", request_id="req.1", session_id=ADEN)

    assert member.engine.calls[0] == {
        "op": "send",
        "app": "claude-code",
        "person": PERSON,
        "to": ADEN,
        "text": "hi Aden",
        "request_id": "req.1",
    }
    assert member.sent_to[0]["key"] == "kann" and member.titles[0] == "Claude Code via Ann"
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
    assert str(lost.value) == hosted_mcp._SLOW

    out = await hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN)
    assert out["state"] == "delivered"
    assert list(member.engine.sent) == ["r1"]
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
    with pytest.raises(ToolError, match="INVALID_REQUEST"):  # a room send takes either form, never junk
        await hosted_mcp.send_message(text="hi", request_id="not ok!", room_id=str(uuid.uuid4()))
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.message_receipt("not ok!")
    assert member.engine.ops("unread") == []


@pytest.mark.asyncio
async def test_a_uuid_session_id_is_a_network_address_like_any_other(member):
    sid = str(uuid.uuid4())
    out = await hosted_mcp.send_message(text="hi", request_id="r1", session_id=sid)
    assert out["destination"] == f"network:{sid}"


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
    with pytest.raises(ToolError, match="UNAVAILABLE"):  # the peek failed: nothing was taken
        await hosted_mcp.check_inbox()
    member.engine.no_ack = False
    assert (await hosted_mcp.check_inbox())["messages"] == [_record("q1")]
    assert (await hosted_mcp.check_inbox())["messages"] == []


@pytest.mark.asyncio
async def test_find_sessions_lists_the_network_sessions_in_the_engines_shape(member):
    out = await hosted_mcp.find_sessions(query="aden")
    assert member.engine.calls[0] == {"op": "directory", "app": "claude-code", "person": PERSON, "query": "aden"}
    assert out["sessions"] == [
        {
            "session_id": ADEN,
            "title": "Aden's Mac",
            "name": "claude-5",
            "agent": "claude",
            "online": True,
            "is_this_session": False,
        },
        {
            "session_id": OWN,
            "title": "Claude Code via Ann",
            "agent": "claude-code",
            "online": True,
            "is_this_session": True,
        },
        {"session_id": CODING, "title": "Aden's Claude", "agent": "claude", "online": True, "is_this_session": False},
        {
            "session_id": f"ab12cd34/sleepy-{PERSON}",
            "title": "Sleepy",
            "agent": "claude",
            "online": False,
            "is_this_session": False,
        },
        {
            "session_id": "ab12cd34/terminal",
            "title": "Terminal",
            "agent": "terminal",
            "online": True,
            "is_this_session": False,
        },
    ]
    assert out["total"] == 5 and set(out) == {"sessions", "total"}  # network-only, like the engine's


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
    assert [c["app"] for c in member.engine.calls if c["op"] == "send"] == ["claude", "chatgpt"]
    assert {"Claude via Ann", "ChatGPT via Ann"} <= set(member.titles)


@pytest.mark.asyncio
async def test_a_hung_computer_answers_within_the_budget(member, monkeypatch):
    monkeypatch.setattr(hosted_mcp, "HOSTED_BUDGET_S", 0.3)
    member.engine.hang = asyncio.Event()  # the computer never answers
    with pytest.raises(ToolError) as err:
        await asyncio.wait_for(hosted_mcp.send_message(text="hi", request_id="r1", session_id=ADEN), 5)
    assert str(err.value) == hosted_mcp._SLOW
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
    member.engine.mail = [_record("q1")]
    member.engine.calls.clear()
    assert "unread" not in await hosted_mcp.find_rooms()
    assert member.engine.calls == []  # a room read never touches the runtime
    assert (await hosted_mcp.message_receipt("nope"))["unread"].startswith("1 unread message")


@pytest.mark.asyncio
async def test_a_uuid_shaped_network_request_id_reads_its_own_receipt(member):
    rid = str(uuid.uuid4()).upper()
    await hosted_mcp.send_message(text="hi", request_id=rid, session_id=ADEN)
    assert (await hosted_mcp.message_receipt(rid))["state"] == "delivered"


@pytest.mark.asyncio
async def test_an_offline_pinned_computer_is_named_and_no_other_stands_in(member):
    member.rows.clear()
    with pytest.raises(ToolError) as err:
        await hosted_mcp.send_message(text="hi", request_id="r9", session_id=ADEN)
    assert str(err.value) == "NETWORK_OFFLINE: Ann's Mac is offline. Open it."
    with pytest.raises(ToolError, match="NETWORK_OFFLINE: Ann's Mac is offline"):  # network-only: it says why
        await hosted_mcp.find_sessions()


@pytest.mark.asyncio
async def test_no_field_overrides_the_app_session(member):
    await network_route.app_call(
        USER,
        "claude-code",
        "Claude Code",
        "inbox",
        5,
        route={"kind": "computer", "key": "kann"},
        app="other",
        person="deadbeef",
        peek=True,
    )
    call = member.engine.calls[-1]
    assert (call["app"], call["person"], call["peek"]) == ("claude-code", PERSON, True)
    assert member.titles[-1] == "Claude Code via Ann"


@pytest.mark.asyncio
async def test_assign_sends_an_assignment_through_the_pinned_computer(member):
    out = await hosted_mcp.assign(request_id="as1", to=ADEN, brief="do x", done_when="x done", deadline_s=300)

    assert member.engine.calls[0] == {
        "op": "send",
        "app": "claude-code",
        "person": PERSON,
        "to": ADEN,
        "text": "do x",
        "request_id": "as1",
        "kind": "assign",
        "done_when": "x done",
        "deadline_s": 300,
    }
    assert out["destination"] == f"network:{ADEN}" and out["requestId"] == "as1"
    with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
        await hosted_mcp.assign(request_id="as1", to=OWN, brief="do y", done_when="y done")


@pytest.mark.asyncio
async def test_a_reply_with_a_status_is_a_result(member):
    await hosted_mcp.send_message(text="x done", request_id="re1", reply_to="as1", status="failed")
    call = member.engine.calls[0]
    assert call["kind"] == "result" and call["status"] == "failed" and call["reply_to"] == "as1"

    before = list(member.engine.calls)
    with pytest.raises(ApiProblem) as refused:
        await hosted_mcp.send_message(text="?", request_id="r9", room_id=str(uuid.uuid4()), status="done")
    assert refused.value.error.code == "INVALID_REQUEST" and member.engine.calls == before
    with pytest.raises(ApiProblem) as bare:
        await hosted_mcp.send_message(text="x", request_id="r10", session_id=ADEN, status="done")
    assert bare.value.error.code == "INVALID_REQUEST" and member.engine.calls == before


@pytest.mark.asyncio
async def test_cancel_assignment_names_the_assignment_and_sends_no_to(member):
    out = await hosted_mcp.cancel_assignment(assignment="as1", request_id="c1")
    assert member.engine.calls[0] == {
        "op": "send",
        "app": "claude-code",
        "person": PERSON,
        "reply_to": "as1",
        "text": "cancelled",
        "request_id": "c1",
        "kind": "cancel",
    }
    assert out["destination"] == "network:reply:as1"
    await hosted_mcp.cancel_assignment(assignment="as1", request_id="c2", note="no longer needed")
    assert [c for c in member.engine.calls if c["op"] == "send"][1]["text"] == "no longer needed"


@pytest.mark.asyncio
async def test_find_workers_lists_online_always_on_agents_only(member):
    out = await hosted_mcp.find_workers()
    assert [r["session_id"] for r in out["sessions"]] == [ADEN, OWN]  # name addresses, online
    assert all(r["always_on"] is True for r in out["sessions"]) and out["total"] == 2
    one = await hosted_mcp.find_workers(limit=0)
    assert len(one["sessions"]) == 1 and one["total"] == 2


@pytest.mark.asyncio
async def test_find_workers_lists_computers_that_start_workers_first(member, monkeypatch):
    rows = [
        {"session_id": "abc/friday", "online": True},
        {"session_id": "abc/claude:1", "online": True},
        {"session_id": "abc/opencode:new", "online": True, "kind": "worker", "free": 2},
        {"session_id": "off/opencode:new", "online": False, "kind": "worker"},
        # Through the app link ``kind`` is stripped: ``free`` still says worker host.
        {"session_id": "lnk/codex:new", "online": True, "free": 1},
    ]

    async def sessions(auth, query):
        return rows

    monkeypatch.setattr(hosted_mcp, "_network_sessions", sessions)
    out = await hosted_mcp.find_workers()
    assert [(r["session_id"], r["always_on"]) for r in out["sessions"]] == [
        ("abc/opencode:new", False),
        ("lnk/codex:new", False),
        ("abc/friday", True),
    ]


@pytest.mark.asyncio
async def test_a_result_record_and_a_receipts_result_pass_through(member):
    member.engine.mail = [
        {**_record("re1", "x done"), "kind": "result", "status": "done", "reply_to": "m-as1", "framed": "[result]"}
    ]
    got = (await hosted_mcp.check_inbox())["messages"][0]
    assert (got["kind"], got["status"], got["reply_to"], got["framed"]) == ("result", "done", "m-as1", "[result]")

    await hosted_mcp.assign(request_id="as1", to=ADEN, brief="do x", done_when="x done")
    member.engine.results["as1"] = {"request_id": "res1", "status": "done"}
    assert (await hosted_mcp.message_receipt("as1"))["result"] == {"request_id": "res1", "status": "done"}
