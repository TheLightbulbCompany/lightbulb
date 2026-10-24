"""A hosted assistant (ChatGPT, Claude.ai, Grok...) reaches the lightbulb network THROUGH the
workspace member, as "<Client> via <Person>": the backend holds no network key and dials no Mac."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.services import hosted_mcp, network_member
from core.services.messaging import ApiProblem
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

OWNER, USER = "org_a", "user_ann"
PERSON = network_member.app_person(USER)
STARTING = "RUNTIME_UNAVAILABLE: the workspace is starting; retry with the same request_id"


class Engine:
    """The member's engine behind ``network.app``: ``app.send`` dedupes on request_id."""

    def __init__(self):
        self.calls: list[dict] = []
        self.sent: dict[str, str] = {}
        self.lose_next_answer = False

    async def __call__(self, owner, method, params):
        assert (owner, method) == (OWNER, "network.app")
        self.calls.append(params)
        op = params["op"]
        if op == "send":
            self.sent.setdefault(params["request_id"], params["text"])
            if self.lose_next_answer:
                self.lose_next_answer = False
                raise asyncio.TimeoutError("network.app timed out")
            return {"state": "delivered"}
        if op == "receipt":
            return {"state": "delivered" if params["request_id"] in self.sent else "unknown"}
        if op == "inbox":
            return [{"from": "aden-mac", "text": "hello"}]
        return [{"name": "aden-mac", "title": "Aden's Mac"}]


@pytest.fixture
def member(table, monkeypatch):
    """A workspace that is on the network, called by Ann's Claude Code."""
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(network_member.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr("core.services.hermes_profiles.put_owner_object", lambda k, b: objects.__setitem__(k, b))
    monkeypatch.setattr("core.services.hermes_profiles.get_owner_object", lambda k: objects.get(k))
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ann Lee"))
    objects[network_member.sessions_key(OWNER)] = json.dumps({"workspace": "Acme", "sessions": []}).encode()
    events: list[str] = []
    engine = Engine()

    async def rpc(owner, method, params):
        events.append(params["op"])
        return await engine(owner, method, params)

    monkeypatch.setattr(network_member.container_rpc, "call", rpc)
    monkeypatch.setattr(network_member, "publish_sessions", AsyncMock(side_effect=lambda _o: events.append("publish")))
    token = SimpleNamespace(
        client_id="https://claude.ai/oauth/claude-code-client-metadata",
        scopes=[hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        subject=USER,
        claims={"org_id": OWNER},
    )
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: token)
    return SimpleNamespace(engine=engine, events=events, objects=objects, table=table)


def _app_entries(objects):
    doc = json.loads(objects[network_member.sessions_key(OWNER)])
    return [s for s in doc["sessions"] if "agentId" not in s]


def _pins(table):
    return [i for i in table.scan()["Items"] if str(i["sk"]).startswith("MCPSEND#")]


@pytest.mark.asyncio
async def test_a_network_send_declares_the_app_session_then_sends(member):
    rid = uuid.uuid4()
    out = await hosted_mcp.send_message(text="hi Aden", request_id=rid, network_to="aden-mac")

    assert _app_entries(member.objects) == [
        {
            "name": f"claude-code-{PERSON}",
            "agent": "claude-code",
            "title": "Claude Code via Ann",
            "app": "claude-code",
            "person": PERSON,
        }
    ]
    assert member.events == ["publish", "send"]
    assert member.engine.calls == [
        {
            "op": "send",
            "app": "claude-code",
            "person": PERSON,
            "to": "aden-mac",
            "text": "hi Aden",
            "request_id": str(rid),
        }
    ]
    assert out == {"destination": "network:aden-mac", "requestId": str(rid), "state": "delivered"}


@pytest.mark.asyncio
async def test_a_retry_with_the_same_request_id_sends_once(member):
    rid = uuid.uuid4()
    member.engine.lose_next_answer = True
    with pytest.raises(ToolError) as lost:
        await hosted_mcp.send_message(text="hi", request_id=rid, network_to="aden-mac")
    assert str(lost.value) == STARTING

    out = await hosted_mcp.send_message(text="hi", request_id=rid, network_to="aden-mac")
    assert out["state"] == "delivered"
    assert list(member.engine.sent) == [str(rid)]
    assert {c["request_id"] for c in member.engine.calls} == {str(rid)}
    assert member.events.count("publish") == 1  # the app session was new only the first time
    assert len(_pins(member.table)) == 1
    receipt = await hosted_mcp.message_receipt(rid)
    assert receipt == {"destination": "network:aden-mac", "requestId": str(rid), "state": "delivered"}


@pytest.mark.asyncio
async def test_network_to_is_exclusive_with_the_other_targets(member):
    with pytest.raises(ApiProblem) as both:
        await hosted_mcp.send_message(text="hi", request_id=uuid.uuid4(), room_id=str(uuid.uuid4()), network_to="x")
    assert both.value.error.code == "INVALID_REQUEST" and "network_to" in both.value.error.message
    rid = uuid.uuid4()
    await hosted_mcp.send_message(text="hi", request_id=rid, network_to="aden-mac")
    with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
        await hosted_mcp.send_message(text="hi", request_id=rid, network_to="someone-else")
    assert member.engine.calls[-1]["to"] == "aden-mac"


@pytest.mark.asyncio
async def test_the_receipt_reads_back_through_the_member(member):
    rid = uuid.uuid4()
    await hosted_mcp.send_message(text="hi", request_id=rid, network_to="aden-mac")
    receipt = await hosted_mcp.message_receipt(rid)
    assert member.engine.calls[-1] == {"op": "receipt", "app": "claude-code", "person": PERSON, "request_id": str(rid)}
    assert receipt == {"destination": "network:aden-mac", "requestId": str(rid), "state": "delivered"}


@pytest.mark.asyncio
async def test_inbox_and_directory_return_engine_records_and_store_nothing(member):
    assert await hosted_mcp.network("inbox") == [{"from": "aden-mac", "text": "hello"}]
    assert await hosted_mcp.network("directory", query="aden") == [{"name": "aden-mac", "title": "Aden's Mac"}]
    assert member.engine.calls == [
        {"op": "inbox", "app": "claude-code", "person": PERSON},
        {"op": "directory", "app": "claude-code", "person": PERSON, "query": "aden"},
    ]
    assert _pins(member.table) == []
