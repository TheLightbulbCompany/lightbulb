"""The hosted MCP serves the engine's tool definitions (``network_tools.json``, vendored from the
engine's ``src/mcp/tools.json`` at the pin): the same names, descriptions and input schemas, plus
``wake_me`` alone. A coding session's own sends go through its computer's engine, never here."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.auth import AuthContext
from core.services import hosted_mcp

SPEC = json.loads((Path(hosted_mcp.__file__).with_name("network_tools.json")).read_text())


def _shape(schema: dict) -> dict:
    """{property: type} and required, with pydantic's ``anyOf [T, null]`` read as T."""
    types = {}
    for name, prop in schema["properties"].items():
        options = prop.get("anyOf", [prop])
        kinds = [o for o in options if o.get("type") != "null"]
        assert len(kinds) == 1, name
        kind = kinds[0]
        types[name] = (kind.get("type"), kind.get("items", {}).get("type"), tuple(kind.get("enum") or ()))
    return {"types": types, "required": sorted(schema.get("required", []))}


@pytest.mark.asyncio
async def test_hosted_mcp_tools_match_network_tools_json():
    tools = {t.name: t for t in await hosted_mcp.mcp.list_tools()}
    # use_browser moves a coding session's browser: engine only.
    assert set(tools) == {t["name"] for t in SPEC if t["name"] != "use_browser"} | {"wake_me"}
    for spec in (t for t in SPEC if t["name"] != "use_browser"):
        tool = tools[spec["name"]]
        assert tool.description == spec["description"], spec["name"]
        assert _shape(tool.input_schema) == _shape(spec["inputSchema"]), spec["name"]
    assert {t["scope"] for t in SPEC} == {"network", "workspace"}


@pytest.mark.asyncio
async def test_hosted_send_message_has_no_session_uuid_or_agent_branch(monkeypatch):
    props = {t.name: t for t in await hosted_mcp.mcp.list_tools()}["send_message"].input_schema["properties"]
    assert "agent_id" not in props
    # A UUID session_id is a network address like any other: it goes to the engine, never to a hub session.
    through = AsyncMock(return_value={"state": "sent"})
    monkeypatch.setattr(hosted_mcp, "_net_caller", lambda *_: AuthContext(user_id="user_a", org_id="org_1"))
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: type("T", (), {"client_id": "c", "claims": {}})())
    monkeypatch.setattr(hosted_mcp, "_sender", lambda _t: ("claude", "Claude"))
    monkeypatch.setattr(hosted_mcp, "_pin_destination", AsyncMock(return_value={"app": "claude"}))
    monkeypatch.setattr(hosted_mcp, "_through_member", through)
    sid = "11111111-1111-4111-8111-111111111111"
    result = await hosted_mcp.send_message.__wrapped__(text="hi", request_id="r1", session_id=sid)
    assert result["destination"] == f"network:{sid}"
    assert through.await_args.args[1] == "send" and through.await_args.kwargs["to"] == sid


@pytest.mark.asyncio
async def test_find_sessions_is_network_only(monkeypatch):
    monkeypatch.setattr(hosted_mcp, "_net_caller", lambda *_: AuthContext(user_id="user_a", org_id="org_1"))
    found = [{"session_id": "ab12cd34/claude-5", "is_this_session": False}]
    monkeypatch.setattr(hosted_mcp, "_network_sessions", AsyncMock(return_value=found))
    assert await hosted_mcp.find_sessions.__wrapped__() == {"sessions": found, "total": 1}


def test_a_room_send_takes_the_engines_request_id_format():
    auth = AuthContext(user_id="user_a", org_id="org_1")
    derived = hosted_mcp._uuid_request_id(auth, "turn-7.net.1")
    assert derived == hosted_mcp._uuid_request_id(auth, "turn-7.net.1")  # a retry reuses it
    assert derived != hosted_mcp._uuid_request_id(AuthContext(user_id="user_b"), "turn-7.net.1")
    given = "11111111-1111-4111-8111-111111111111"
    assert str(hosted_mcp._uuid_request_id(auth, given)) == given
    with pytest.raises(ToolError):
        hosted_mcp._uuid_request_id(auth, "not ok!")


@pytest.mark.asyncio
async def test_every_tool_declares_openais_three_hints():
    """OpenAI's plugin review needs explicit readOnly/destructive/openWorld booleans on every tool.
    openWorld is true where a call leaves the bounded workspace: a message other people (or their
    sessions) act on, a browser on the public web, a webhook at any URL."""
    hints = {
        t.name: (t.annotations.read_only_hint, t.annotations.destructive_hint, t.annotations.open_world_hint)
        for t in await hosted_mcp.mcp.list_tools()
    }
    reads = {
        "find_sessions",
        "message_receipt",
        "check_inbox",
        "find_rooms",
        "read_messages",
        "find_tasks",
        "read_task",
        "recall",
    }
    assert hints == {
        **{name: (True, False, False) for name in reads},
        "send_message": (False, False, True),  # deletes and overwrites nothing (owner, 10-27)
        "update_task": (False, False, False),
        "remember": (False, False, False),
        "wake_me": (False, False, True),
    }


@pytest.mark.asyncio
async def test_a_question_recall_is_one_page_of_text_and_counts(monkeypatch):
    from datetime import UTC, datetime

    from core.services import memory_api
    from core.services.memory_stages import Candidate, compose

    at = datetime(2026, 10, 29, tzinfo=UTC)
    claims = [Candidate(f"c{i}", "claim", f"Ann: topic {i}: value {i}", at) for i in range(60)]
    turns = [Candidate(f"t{i}", "turn", "Ann: " + "x" * 795, at) for i in range(60)]
    full = compose(claims + turns, 8000, at)
    asked = AsyncMock(return_value=full)
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_: AuthContext(user_id="user_a", org_id="org_1"))
    monkeypatch.setattr(memory_api, "recall_page", asked)
    out = await hosted_mcp.recall.__wrapped__(question="what topics?")
    assert set(out) == {"page", "counts"} and isinstance(out["page"], str)
    assert out["counts"] == {"facts": 50, "turns": len(full["results"]) - 50}
    assert "Ann: topic 0: value 0" in out["page"] and "Turns:" in out["page"]
    assert 7000 < -(-len(json.dumps(out)) // 4) <= 9000  # a full page, about 8,000 tokens
    assert asked.await_args.args == ("org_1", "user_a", "what topics?")
