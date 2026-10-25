"""Agent network §4: every lightbulb tool result for a shared-session caller carries ``unread``
while that session has sends it has not been given yet (Cursor and Grok CLI cannot be woken)."""

import time
from unittest.mock import AsyncMock

import pytest

from core.services import hosted_mcp
from tests.unit.services.test_hosted_mcp_session_caller import SOURCE_ID, as_session  # noqa: F401


def _command(state="pending", **extra):
    return {"kind": "send", "state": state, "deliverBy": int(time.time()) + 600, **extra}


@pytest.fixture
def commands(monkeypatch):
    rows: list[dict] = []
    listed = AsyncMock(side_effect=lambda owner, sid: rows if sid == SOURCE_ID else [])
    monkeypatch.setattr(hosted_mcp.local_sessions_repo, "list_commands", listed)
    monkeypatch.setattr(hosted_mcp, "rooms_for", AsyncMock(return_value={"rooms": []}))
    return rows


@pytest.mark.asyncio
async def test_a_session_caller_with_undelivered_sends_sees_unread(as_session, commands):  # noqa: F811
    await as_session()
    commands.extend([_command(), _command("dispatched")])
    result = await hosted_mcp.find_rooms()
    assert result["rooms"] == []
    assert result["unread"].startswith("2 unread messages")


@pytest.mark.asyncio
async def test_no_note_when_nothing_waits(as_session, commands):  # noqa: F811
    await as_session()
    commands.extend(
        [
            _command("applied"),
            _command(heldFor="user_x", state="awaiting_approval"),  # held for the owner's Allow
            _command(deliverBy=int(time.time()) - 1),  # expired
            {**_command(), "kind": "interrupt"},
        ]
    )
    assert "unread" not in await hosted_mcp.find_rooms()


@pytest.mark.asyncio
async def test_a_person_caller_never_gets_the_note(monkeypatch, commands):
    monkeypatch.setattr(hosted_mcp, "_mac", lambda: None)
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_: None)
    monkeypatch.setattr(hosted_mcp, "_net_caller", lambda *_: None)
    commands.append(_command())
    assert "unread" not in await hosted_mcp.find_rooms()


@pytest.mark.asyncio
async def test_a_failed_count_never_fails_the_tool(as_session, monkeypatch):  # noqa: F811
    await as_session()
    monkeypatch.setattr(hosted_mcp, "rooms_for", AsyncMock(return_value={"rooms": []}))
    monkeypatch.setattr(hosted_mcp.local_sessions_repo, "list_commands", AsyncMock(side_effect=RuntimeError("db")))
    assert await hosted_mcp.find_rooms() == {"rooms": []}


@pytest.mark.asyncio
async def test_the_decorator_keeps_every_tool_schema():
    tools = {t.name: t for t in await hosted_mcp.mcp.list_tools()}
    assert set(tools["send_message"].input_schema["required"]) == {"text", "request_id"}
    assert "session_id" in tools["send_message"].input_schema["properties"]
    assert tools["read_messages"].input_schema["required"] == ["room_id"]
    assert len(tools) == 11 and all(t.description for t in tools.values())
