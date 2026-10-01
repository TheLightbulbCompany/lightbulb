import uuid
from types import SimpleNamespace
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


def test_source_write_fence_tracks_authority_without_heartbeat_revision(monkeypatch):
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(name="test-messaging"))
    source = {
        "pk": "LOCALSESSION#org_a#source",
        "generation": "g1",
        "connectionId": "connection_a",
        "deviceId": "mac_a",
        "humanUserId": "user_a",
        "provider": "codex",
        "nativeConversationId": "thread_a",
        "activeTurnId": "turn_a",
    }
    check = tools.sessions_repo.native_source_condition(source)["ConditionCheck"]
    expression = check["ConditionExpression"]
    assert "revision" not in expression
    assert "generation = :generation" in expression
    assert "connectionId = :connection" in expression
    assert "activeTurnId = :turn" in expression
    assert "attribute_not_exists(unshared)" in expression
    assert "attribute_not_exists(endedAt)" in expression


@pytest.mark.asyncio
async def test_people_targets_require_an_existing_direct_message(monkeypatch):
    channel_person = SimpleNamespace(kind="human", id="user_channel", display_name="Channel", mention_handle="channel")
    dm_person = SimpleNamespace(kind="human", id="user_dm", display_name="DM", mention_handle="dm")
    rooms = [
        SimpleNamespace(room_id=uuid.uuid4(), name="Team", kind="channel", archived_at=None, members=[channel_person])
        for _ in range(21)
    ] + [SimpleNamespace(room_id=uuid.uuid4(), name=None, kind="dm", archived_at=None, members=[dm_person])]
    monkeypatch.setattr(tools.messaging, "list_rooms", AsyncMock(return_value=SimpleNamespace(rooms=rooms)))
    monkeypatch.setattr(
        tools.local_sessions, "list_workspace_sessions", AsyncMock(return_value=SimpleNamespace(sessions=[]))
    )
    targets = await tools._targets(tools.AuthContext(user_id="user_owner", org_id="org_a"), "source")
    assert len(targets["rooms"]) == 20
    assert [person["id"] for person in targets["people"]] == ["user_dm"]


@pytest.mark.asyncio
async def test_session_targets_exclude_offline_and_unsendable_sessions(monkeypatch):
    def session(status: str, send_mode: str):
        return SimpleNamespace(
            session_id=uuid.uuid4(),
            provider="codex",
            title="Target",
            status=status,
            capabilities_verified=True,
            capabilities=SimpleNamespace(send_mode=send_mode),
            human_user_id="user_owner",
            team_control=False,
            allowed_senders=[],
        )

    offline, unsendable, reachable = session("offline", "queue"), session("idle", "none"), session("idle", "queue")
    monkeypatch.setattr(tools.messaging, "list_rooms", AsyncMock(return_value=SimpleNamespace(rooms=[])))
    monkeypatch.setattr(
        tools.local_sessions,
        "list_workspace_sessions",
        AsyncMock(return_value=SimpleNamespace(sessions=[offline, unsendable, reachable])),
    )
    targets = await tools._targets(tools.AuthContext(user_id="user_owner", org_id="org_a"), "source")
    assert [entry["destination"] for entry in targets["sessions"]] == [f"session:{reachable.session_id}"]


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
async def test_claude_source_accepts_its_provider_null_turn_after_exact_session_binding(monkeypatch):
    from core.services import local_session_relay

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
        "provider": "claude",
        "nativeConversationId": "conversation_a",
        "activeTurnId": None,
        "onlineUntil": 9999999999,
        "connectionId": "connection_a",
    }
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    relay = type("Relay", (), {"connect": AsyncMock(return_value=({"device_id": "mac_a", "user_id": "user_a"}, {}))})()
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="claude",
        source_conversation_id="conversation_a",
        source_turn_id=None,
        origin_item_id=None,
        tool="list_targets",
        arguments={},
    )
    assert await tools._source(device, request) == row


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


@pytest.mark.asyncio
async def test_failed_admission_leaves_a_rejected_receipt(monkeypatch):
    sid, target, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    destination = f"session:{target}"
    device = MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )
    source = {"ownerId": "org_a", "sessionId": str(sid), "generation": "g1"}
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(tools, "_claim", AsyncMock(return_value={"destination": destination, "state": "pending"}))
    monkeypatch.setattr(tools, "_committed_result", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tools, "_session_send", AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_OFFLINE", "Target is offline."))
    )
    finish = AsyncMock(return_value={"state": "rejected", "destination": destination})
    monkeypatch.setattr(tools, "_finish", finish)
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="claude",
        source_conversation_id="conversation_a",
        source_turn_id=None,
        tool="send",
        arguments={"destination": destination, "text": "hello", "requestId": str(request_id)},
    )
    with pytest.raises(ApiProblem) as error:
        await tools.execute(device, request)
    assert error.value.error.code == "LOCAL_SESSION_OFFLINE"
    persisted = finish.await_args.args[-1]
    assert persisted["state"] == "rejected"
    assert persisted["error"]["code"] == "LOCAL_SESSION_OFFLINE"


@pytest.mark.asyncio
async def test_replayed_rejection_does_not_send_again(monkeypatch):
    sid, target, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    device = MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(
        tools,
        "_claim",
        AsyncMock(
            return_value={
                "destination": f"session:{target}",
                "result": {
                    "state": "rejected",
                    "error": {"code": "LOCAL_SESSION_OFFLINE", "message": "Target is offline."},
                },
            }
        ),
    )
    send = AsyncMock()
    monkeypatch.setattr(tools, "_session_send", send)
    monkeypatch.setattr(tools, "_committed_result", AsyncMock(return_value=None))
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="claude",
        source_conversation_id="conversation_a",
        source_turn_id=None,
        tool="send",
        arguments={"destination": f"session:{target}", "text": "hello", "requestId": str(request_id)},
    )
    with pytest.raises(ApiProblem) as error:
        await tools.execute(device, request)
    assert error.value.error.code == "LOCAL_SESSION_OFFLINE"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_commit_is_reconciled_before_rejecting_a_receipt(monkeypatch):
    source, room_id, request_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    receipt = {"messageId": str(uuid.uuid4())}
    get = AsyncMock(return_value=receipt)
    monkeypatch.setattr(tools.messaging_repo, "_get", get)
    result = await tools._committed_result("org_a", source, f"room:{room_id}", request_id)
    assert result == {"state": "applied", "destination": f"room:{room_id}", "messageId": receipt["messageId"]}
    get.assert_awaited_once_with(
        tools.messaging_repo.room_pk(room_id), tools.messaging_repo.agent_idemp_sk(f"native:{source}:{request_id}")
    )
