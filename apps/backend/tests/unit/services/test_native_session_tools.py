import uuid
from unittest.mock import AsyncMock

import pytest

from core.services import native_session_tools as tools
from core.services.machine_auth import MachineIdentity
from core.services.messaging import ApiProblem


def test_native_tool_arguments_cannot_supply_an_actor():
    with pytest.raises(ApiProblem):
        tools._send_args(
            {"destination": "session:s", "text": "hello", "requestId": str(uuid.uuid4()), "actorUserId": "owner"}
        )


@pytest.mark.asyncio
async def test_source_requires_the_exact_live_device_generation_and_turn(monkeypatch):
    sid = uuid.uuid4()
    device = MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )
    row = {
        "ownerId": "org_a",
        "sessionId": str(sid),
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "generation": "g1",
        "provider": "codex",
        "nativeConversationId": "thread_a",
        "activeTurnId": "turn_a",
        "onlineUntil": 9999999999,
        "connectionId": "connection_a",
    }
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="codex",
        source_conversation_id="thread_a",
        source_turn_id="wrong",
        origin_item_id="item_a",
        tool="list_targets",
        arguments={},
    )
    with pytest.raises(ApiProblem):
        await tools._source(device, request)


@pytest.mark.asyncio
async def test_native_reply_chain_stops_after_two_hops(monkeypatch):
    source = {"ownerId": "org_a", "sessionId": "source"}
    monkeypatch.setattr(
        tools.sessions_repo,
        "list_commands",
        AsyncMock(
            return_value=[
                {"commandSeq": 1, "actor": {"kind": "session", "id": "first"}, "nativeHop": 1, "state": "applied"},
            ]
        ),
    )
    assert await tools._outgoing_hop(source) == 2
    tools.sessions_repo.list_commands.return_value.append(
        {"commandSeq": 2, "actor": {"kind": "session", "id": "second"}, "nativeHop": 2, "state": "applied"}
    )
    with pytest.raises(ApiProblem):
        await tools._outgoing_hop(source)
