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
    approved = tools.sessions_repo.native_source_condition(source, approval_id="appr-0123456789abcdef0123456789abcdef")
    assert "activeTurnId = :turn" not in approved["ConditionCheck"]["ConditionExpression"]
    assert "nativeSendIntent.approvalId = :approval" in approved["ConditionCheck"]["ConditionExpression"]
    assert "nativeSendIntent.expiresAt > :now" in approved["ConditionCheck"]["ConditionExpression"]


@pytest.mark.asyncio
async def test_codex_send_prepares_private_intent_without_dispatch(monkeypatch):
    sid, room, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    device = MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )
    source = {
        "pk": tools.sessions_repo.session_pk("org_a", str(sid)),
        "sk": "META",
        "ownerId": "org_a",
        "sessionId": str(sid),
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "generation": "g1",
        "provider": "codex",
        "nativeConversationId": "thread_a",
        "activeTurnId": "turn_a",
        "connectionId": "connection_a",
        "revision": "r1",
    }
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="codex",
        source_conversation_id="thread_a",
        source_turn_id="turn_a",
        tool="send",
        arguments={"destination": f"room:{room}", "text": "private body", "requestId": str(request_id)},
    )
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(tools.messaging_repo, "_get", AsyncMock(return_value=None))
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(name="test-messaging"))
    monkeypatch.setattr(tools.sessions_repo, "get_enrollment", AsyncMock(return_value={"revision": "er1"}))
    put = AsyncMock()
    monkeypatch.setattr(tools.sessions_repo, "put_session", put)
    monkeypatch.setattr(tools, "_publish_session", AsyncMock())
    result = await tools.prepare_codex_send(device, request)
    updated, previous, enrollment = put.await_args.args
    claim, intent, audit = [action["Put"]["Item"] for action in put.await_args.kwargs["extra"]]
    assert result["state"] == "pending_approval"
    assert previous is source and enrollment == {"revision": "er1"}
    assert updated["pendingApproval"]["summary"] == "Lightbulb message"
    assert "private body" not in str(updated)
    assert "private body" not in str(claim)
    assert intent["text"] == "private body" and intent["expiresAt"] - intent["createdAt"] == 55 * 60
    assert intent["inputDigest"] == updated["pendingApproval"]["inputDigest"]
    assert audit["action"] == "native_send_requested" and "private body" not in str(audit)


@pytest.mark.asyncio
async def test_codex_send_approval_fails_closed_when_mac_is_offline(monkeypatch):
    sid, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    approval_id = "appr-0123456789abcdef0123456789abcdef"
    digest = tools._intent_digest({"destination": "room:r", "text": "private body", "requestId": request_id})
    pointer = {
        "requestId": request_id,
        "approvalId": approval_id,
        "inputDigest": digest,
        "expiresAt": 9999999999,
        "state": "pending_approval",
        "connectionId": "connection_a",
        "sourceTurnId": "turn_a",
    }
    source = {
        "ownerId": "org_a",
        "sessionId": sid,
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "provider": "codex",
        "onlineUntil": 0,
        "nativeSendIntent": pointer,
        "generation": "g1",
        "nativeConversationId": "thread_a",
        "connectionId": "connection_a",
    }
    claim = {
        "approvalId": approval_id,
        "inputDigest": digest,
        "destination": "room:r",
        "requestHash": tools.messaging_repo.request_hash(sid, "room:r", "private body"),
        "sourceTurnId": "turn_a",
        "originItemId": None,
    }
    intent = {
        "approvalId": approval_id,
        "inputDigest": digest,
        "destination": "room:r",
        "text": "private body",
        "expiresAt": 9999999999,
        "sourceGeneration": "g1",
        "sourceConversationId": "thread_a",
        "requestId": request_id,
        "connectionId": "connection_a",
        "sourceTurnId": "turn_a",
        "originItemId": None,
        "deviceId": "mac_a",
        "humanUserId": "user_a",
    }
    monkeypatch.setattr(tools.local_sessions, "authorize_session", AsyncMock(return_value=(source, {})))
    monkeypatch.setattr(tools.messaging_repo, "_get", AsyncMock(side_effect=[claim, intent]))
    put = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(tools.sessions_repo, "put_session", put)
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)
    decision = tools.NativeSendDecision(
        request_id=request_id, approval_id=approval_id, input_digest=digest, decision="allow"
    )
    with pytest.raises(ApiProblem):
        await tools.answer_codex_send(tools.AuthContext(user_id="user_a", org_id="org_a"), sid, decision)
    put.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_codex_immediate_send_door_is_closed(monkeypatch):
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
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    claim = AsyncMock()
    monkeypatch.setattr(tools, "_claim", claim)
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="codex",
        source_conversation_id="thread_a",
        source_turn_id="turn_a",
        tool="send",
        arguments={"destination": f"room:{uuid.uuid4()}", "text": "must not send", "requestId": str(uuid.uuid4())},
    )
    with pytest.raises(ApiProblem):
        await tools.execute(device, request)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_owner_approval_dispatches_only_the_exact_prepared_send(monkeypatch):
    from core.services import local_session_relay

    sid, room, request_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    approval_id = "appr-0123456789abcdef0123456789abcdef"
    destination = f"room:{room}"
    digest = tools._intent_digest({"destination": destination, "text": "private body", "requestId": request_id})
    pointer = {
        "requestId": request_id,
        "approvalId": approval_id,
        "inputDigest": digest,
        "expiresAt": 9999999999,
        "state": "pending_approval",
        "connectionId": "connection_a",
        "sourceTurnId": "turn_a",
    }
    source = {
        "pk": tools.sessions_repo.session_pk("org_a", sid),
        "sk": "META",
        "ownerId": "org_a",
        "sessionId": sid,
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "provider": "codex",
        "onlineUntil": 9999999999,
        "nativeSendIntent": pointer,
        "generation": "g1",
        "nativeConversationId": "thread_a",
        "connectionId": "connection_a",
        "revision": "r1",
    }
    claim = {
        "approvalId": approval_id,
        "inputDigest": digest,
        "destination": destination,
        "requestHash": tools.messaging_repo.request_hash(sid, destination, "private body"),
        "sourceTurnId": "turn_a",
        "originItemId": None,
    }
    intent = {
        "approvalId": approval_id,
        "inputDigest": digest,
        "requestId": request_id,
        "destination": destination,
        "text": "private body",
        "sourceTurnId": "turn_a",
        "expiresAt": 9999999999,
        "sourceGeneration": "g1",
        "sourceConversationId": "thread_a",
        "connectionId": "connection_a",
        "originItemId": None,
        "deviceId": "mac_a",
        "humanUserId": "user_a",
    }
    auth = tools.AuthContext(user_id="user_a", org_id="org_a")
    monkeypatch.setattr(tools.local_sessions, "authorize_session", AsyncMock(return_value=(source, {"revision": "e1"})))
    monkeypatch.setattr(tools.messaging_repo, "_get", AsyncMock(side_effect=[claim, intent]))
    monkeypatch.setattr(
        tools.messaging_repo, "_table", lambda: SimpleNamespace(name="test-messaging", delete_item=lambda **_: None)
    )
    relay = SimpleNamespace(
        connect=AsyncMock(return_value=({"device_id": "mac_a", "user_id": "user_a"}, {})),
        _connection_check=lambda *_: {"ConditionCheck": {"Key": "relay"}},
    )
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)
    put = AsyncMock()
    dispatch = AsyncMock(return_value={"state": "applied", "destination": destination, "messageId": "message_a"})
    monkeypatch.setattr(tools.sessions_repo, "put_session", put)
    monkeypatch.setattr(tools, "_committed_result", AsyncMock(return_value=None))
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)
    monkeypatch.setattr(tools, "_finish", AsyncMock(return_value=dispatch.return_value))
    monkeypatch.setattr(tools, "_clear_codex_intent", AsyncMock())
    monkeypatch.setattr(tools, "run_in_thread", AsyncMock())
    decision = tools.NativeSendDecision(
        request_id=request_id, approval_id=approval_id, input_digest=digest, decision="allow"
    )
    result = await tools.answer_codex_send(auth, sid, decision)
    assert result["state"] == "applied" and result["messageId"] == "message_a"
    assert put.await_args.args[0]["nativeSendIntent"]["state"] == "dispatching"
    assert dispatch.await_args.kwargs["approval_id"] == approval_id
    assert dispatch.await_args.args[1]["activeTurnId"] == "turn_a"
    assert dispatch.await_args.args[3] == "private body"


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
