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
    assert set(tools) == {t["name"] for t in SPEC} | {"wake_me"}
    for spec in SPEC:
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


@pytest.mark.asyncio
async def test_hosted_use_browser_says_to_call_it_from_the_session(monkeypatch):
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_: AuthContext(user_id="user_a", org_id="org_1"))
    with pytest.raises(ToolError, match="from that session"):
        await hosted_mcp.use_browser.__wrapped__("cloud")


def test_a_room_send_takes_the_engines_request_id_format():
    auth = AuthContext(user_id="user_a", org_id="org_1")
    derived = hosted_mcp._uuid_request_id(auth, "turn-7.net.1")
    assert derived == hosted_mcp._uuid_request_id(auth, "turn-7.net.1")  # a retry reuses it
    assert derived != hosted_mcp._uuid_request_id(AuthContext(user_id="user_b"), "turn-7.net.1")
    given = "11111111-1111-4111-8111-111111111111"
    assert str(hosted_mcp._uuid_request_id(auth, given)) == given
    with pytest.raises(ToolError):
        hosted_mcp._uuid_request_id(auth, "not ok!")
