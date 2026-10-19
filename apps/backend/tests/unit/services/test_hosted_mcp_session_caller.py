"""One tool set, two callers: a paired Mac's shared session reaches /mcp with its device JWT."""

import time
import uuid
from unittest.mock import AsyncMock

import jwt
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from core.services import hosted_mcp
from core.services.machine_auth import MachineIdentity

DEVICE = MachineIdentity(
    user_id="user_mac",
    org_id="org_test",
    owner_id="org_test",
    kind="node",
    auth_kind="device",
    client_id="openclaw-macos",
    device_id="dev_1",
)
SOURCE_ID = str(uuid.uuid4())
SOURCE = {
    "sourceSessionId": SOURCE_ID,
    "sourceGeneration": "g1",
    "sourceProvider": "claude",
    "sourceConversationId": "c1",
    "sourceTurnId": None,
    "originItemId": None,
}


def _token(source=SOURCE):
    now = int(time.time())
    # Unsigned is fine here: verify_upgrade (which checks the signature) is mocked.
    return jwt.encode({"exp": now + 60, "iat": now, "source": source}, "k", algorithm="HS256")


@pytest.fixture
def as_session(monkeypatch):
    """Run tools as the verified Mac session, exactly as the auth middleware would set it."""
    monkeypatch.setattr(hosted_mcp, "verify_upgrade", AsyncMock(return_value=DEVICE))
    monkeypatch.setattr(hosted_mcp.local_sessions, "_enrolled", AsyncMock(return_value={}))

    async def enter(source=SOURCE):
        access = await hosted_mcp.LightbulbVerifier().verify_token(_token(source))
        auth_context_var.set(type("U", (), {"access_token": access})())
        return access

    return enter


@pytest.mark.asyncio
async def test_a_device_token_is_a_session_caller_with_every_scope(as_session):
    access = await as_session()
    assert isinstance(access, AccessToken)
    assert access.subject == "user_mac" and access.resource == hosted_mcp.RESOURCE
    assert {hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE} <= set(access.scopes)
    assert hosted_mcp._mac() == DEVICE
    assert hosted_mcp._caller(hosted_mcp.WRITE_SCOPE).org_id == "org_test"


@pytest.mark.asyncio
async def test_a_device_whose_sharing_is_off_is_refused(monkeypatch):
    monkeypatch.setattr(hosted_mcp, "verify_upgrade", AsyncMock(return_value=DEVICE))
    monkeypatch.setattr(
        hosted_mcp.local_sessions,
        "_enrolled",
        AsyncMock(side_effect=hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", "off")),
    )
    assert await hosted_mcp.LightbulbVerifier().verify_token(_token()) is None


@pytest.mark.asyncio
async def test_an_unpaired_or_personal_device_is_refused(monkeypatch):
    monkeypatch.setattr(hosted_mcp, "verify_upgrade", AsyncMock(return_value=None))
    assert await hosted_mcp.LightbulbVerifier().verify_token(_token()) is None
    personal = MachineIdentity(**{**DEVICE.__dict__, "org_id": ""})
    monkeypatch.setattr(hosted_mcp, "verify_upgrade", AsyncMock(return_value=personal))
    assert await hosted_mcp.LightbulbVerifier().verify_token(_token()) is None


@pytest.mark.asyncio
async def test_a_session_send_takes_the_mac_native_path(as_session, monkeypatch):
    await as_session()
    execute = AsyncMock(return_value={"state": "posted"})
    monkeypatch.setattr(hosted_mcp.native_session_tools, "execute", execute)
    request_id, room_id = uuid.uuid4(), str(uuid.uuid4())
    assert await hosted_mcp.send_message("hi", request_id, room_id=room_id) == {"state": "posted"}
    device, request = execute.await_args.args
    assert device == DEVICE and request.tool == "send"
    assert str(request.source_session_id) == SOURCE_ID
    assert request.arguments == {"destination": f"room:{room_id}", "text": "hi", "requestId": str(request_id)}


@pytest.mark.asyncio
async def test_a_session_send_takes_no_thread_and_needs_its_source(as_session, monkeypatch):
    monkeypatch.setattr(hosted_mcp.native_session_tools, "execute", AsyncMock())
    await as_session()
    with pytest.raises(ToolError, match="INVALID_REQUEST"):
        await hosted_mcp.send_message("hi", uuid.uuid4(), room_id=str(uuid.uuid4()), mention_ids=["x"])
    await as_session(source=None)
    with pytest.raises(ToolError, match="names its session"):
        await hosted_mcp.send_message("hi", uuid.uuid4(), room_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_a_refused_session_send_says_why(as_session, monkeypatch):
    await as_session()
    monkeypatch.setattr(
        hosted_mcp.native_session_tools,
        "execute",
        AsyncMock(side_effect=hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not current.")),
    )
    with pytest.raises(ToolError, match="LOCAL_SESSION_FORBIDDEN: Not current."):
        await hosted_mcp.send_message("hi", uuid.uuid4(), person_id="user_x")


@pytest.mark.asyncio
async def test_a_session_receipt_reads_its_own_ledger(as_session, monkeypatch):
    await as_session()
    execute = AsyncMock(return_value={"state": "posted"})
    monkeypatch.setattr(hosted_mcp.native_session_tools, "execute", execute)
    request_id = uuid.uuid4()
    await hosted_mcp.message_receipt(request_id)
    assert execute.await_args.args[1].tool == "receipt"
    assert execute.await_args.args[1].arguments == {"requestId": str(request_id)}


@pytest.mark.asyncio
async def test_find_sessions_marks_the_calling_session(as_session, monkeypatch):
    await as_session()
    monkeypatch.setattr(
        hosted_mcp,
        "sessions_for",
        AsyncMock(return_value={"sessions": [{"sessionId": SOURCE_ID}, {"sessionId": "other"}], "total": 2}),
    )
    result = await hosted_mcp.find_sessions()
    assert [s["isThisSession"] for s in result["sessions"]] == [True, False]


@pytest.mark.asyncio
async def test_use_browser_from_a_session_moves_that_session(as_session, monkeypatch):
    await as_session()
    place = AsyncMock(return_value={"where": "cloud"})
    monkeypatch.setattr(hosted_mcp.session_browser, "place_from_mac", place)
    assert await hosted_mcp.use_browser("cloud") == {"where": "cloud"}
    place.assert_awaited_once_with("org_test", "user_mac", "dev_1", SOURCE_ID, "cloud")
    with pytest.raises(ToolError, match="omit session_id"):
        await hosted_mcp.use_browser("cloud", session_id=SOURCE_ID)


@pytest.mark.asyncio
async def test_exactly_the_nine_tools_and_only_send_is_destructive():
    listed = {tool.name: tool for tool in await hosted_mcp.mcp.list_tools()}
    assert sorted(listed) == sorted(
        [
            "find_rooms",
            "find_sessions",
            "read_messages",
            "send_message",
            "message_receipt",
            "find_tasks",
            "read_task",
            "update_task",
            "use_browser",
        ]
    )
    for name, tool in listed.items():
        writes = name in {"send_message", "update_task", "use_browser"}
        assert tool.annotations.read_only_hint is (not writes), name
        assert tool.annotations.destructive_hint is (name == "send_message"), name
        assert tool.annotations.open_world_hint is False, name
    # On the wire the hints are camelCase, as MCP clients read them.
    wire = listed["send_message"].model_dump(by_alias=True, exclude_none=True)["annotations"]
    assert wire == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
