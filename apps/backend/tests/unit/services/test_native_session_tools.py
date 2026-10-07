import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


def test_native_session_target_and_source_share_one_pairing_check(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo.container_repo, "_get_table", lambda: SimpleNamespace(name="owners"))
    check = tools.sessions_repo.pairing_condition(
        "org_a", "target_owner", "target_mac", None, also=("source_owner", "source_mac")
    )["ConditionCheck"]
    assert check["Key"] == {"owner_id": "org_a"}
    assert "#pd.#sourceMac.#sourceUid = :sourceUser" in check["ConditionExpression"]
    assert check["ExpressionAttributeNames"]["#sourceMac"] == "source_mac"
    assert check["ExpressionAttributeValues"][":sourceUser"] == "source_owner"


@pytest.mark.asyncio
async def test_native_send_finish_aliases_result_in_write_and_idempotency_check(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(update_item=write))
    monkeypatch.setattr(tools, "run_in_thread", AsyncMock())
    result = {"state": "applied", "destination": "room:r", "messageId": "m"}

    assert await tools._finish("org", "source", str(uuid.uuid4()), "room:r", result) == result
    kwargs = tools.run_in_thread.await_args.kwargs
    assert kwargs["ExpressionAttributeNames"]["#result"] == "result"
    assert "#result" in kwargs["UpdateExpression"]
    assert "attribute_not_exists(#result)" in kwargs["ConditionExpression"]


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
            control_unavailable_reason=None,
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


DEVICE = MachineIdentity(
    user_id="user_a",
    org_id="org_a",
    owner_id="org_a",
    kind="node",
    auth_kind="device",
    client_id="openclaw-macos",
    device_id="mac_a",
)


def _send_request(sid, destination, text, request_id):
    return tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="claude",
        source_conversation_id="conversation_a",
        source_turn_id=None,
        tool="send",
        arguments={"destination": destination, "text": text, "requestId": str(request_id)},
    )


@pytest.mark.asyncio
async def test_native_send_posts_immediately_and_returns_the_message(monkeypatch):
    sid, room, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    destination = f"room:{room}"
    source = {"ownerId": "org_a", "sessionId": str(sid), "generation": "g1"}
    applied = {"state": "applied", "destination": destination, "messageId": str(uuid.uuid4())}
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(put_item=None, update_item=None))
    writes = AsyncMock()
    monkeypatch.setattr(tools, "run_in_thread", writes)
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=source))
    capture = Mock()
    monkeypatch.setattr(tools, "capture_session_event", capture)
    dispatch = AsyncMock(return_value=applied)
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)

    assert await tools.execute(DEVICE, _send_request(sid, destination, "hello", request_id)) == applied
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[1:] == (source, destination, "hello", str(request_id))
    claim, receipt = (call.kwargs for call in writes.await_args_list)
    assert claim["Item"]["state"] == "pending" and "hello" not in str(claim)
    assert receipt["ExpressionAttributeValues"][":result"] == applied
    assert capture.call_args.args[1] == "native_send_outcome"


@pytest.mark.asyncio
async def test_refused_native_send_returns_the_error_and_posts_nothing(monkeypatch):
    sid, request_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(tools, "_claim", AsyncMock(return_value={"destination": "person:user_b"}))
    monkeypatch.setattr(tools.messaging, "list_rooms", AsyncMock(return_value=SimpleNamespace(rooms=[])))
    monkeypatch.setattr(tools, "_committed_result", AsyncMock(return_value=None))
    finish = AsyncMock(side_effect=lambda *args: args[-1])
    monkeypatch.setattr(tools, "_finish", finish)
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)

    with pytest.raises(ApiProblem) as error:
        await tools.execute(DEVICE, _send_request(sid, "person:user_b", "hello", request_id))
    assert error.value.error.code == "ROOM_NOT_FOUND"
    dispatch.assert_not_awaited()
    assert finish.await_args.args[-1]["state"] == "rejected"


@pytest.mark.asyncio
async def test_replayed_native_send_returns_the_same_message(monkeypatch):
    from botocore.exceptions import ClientError

    sid, room, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    destination = f"room:{room}"
    applied = {"state": "applied", "destination": destination, "messageId": str(uuid.uuid4())}
    prior = {
        "destination": destination,
        "requestHash": tools.messaging_repo.request_hash(str(sid), destination, "hello"),
        "result": applied,
    }
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(put_item=None))
    conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    monkeypatch.setattr(tools, "run_in_thread", AsyncMock(side_effect=conflict))
    monkeypatch.setattr(tools.messaging_repo, "_get", AsyncMock(return_value=prior))
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)

    assert await tools.execute(DEVICE, _send_request(sid, destination, "hello", request_id)) == applied
    dispatch.assert_not_awaited()
    with pytest.raises(ApiProblem) as error:
        await tools.execute(DEVICE, _send_request(sid, destination, "different", request_id))
    assert error.value.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_pending_legacy_receipt_recovers_a_committed_room_message(monkeypatch):
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
    destination = f"room:{target}"
    committed = {"state": "applied", "destination": destination, "messageId": str(uuid.uuid4())}
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(
        tools.messaging_repo, "_get", AsyncMock(return_value={"destination": destination, "state": "pending"})
    )
    monkeypatch.setattr(tools, "_committed_result", AsyncMock(return_value=committed))
    finish = AsyncMock(return_value=committed)
    monkeypatch.setattr(tools, "_finish", finish)
    request = tools.NativeToolRequest(
        source_session_id=sid,
        source_generation="g1",
        source_provider="claude",
        source_conversation_id="conversation_a",
        source_turn_id=None,
        tool="receipt",
        arguments={"requestId": str(request_id)},
    )
    assert await tools.execute(device, request) == committed
    finish.assert_awaited_once_with("org_a", str(sid), str(request_id), destination, committed)


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


@pytest.mark.asyncio
async def test_replayed_legacy_denial_returns_its_stored_receipt(monkeypatch):
    sid, request_id = uuid.uuid4(), uuid.uuid4()
    denied = {
        "state": "rejected",
        "destination": "room:r",
        "error": {"code": "OWNER_DENIED", "message": "The owner denied this send."},
    }
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(tools, "_claim", AsyncMock(return_value={"destination": "room:r", "result": denied}))
    monkeypatch.setattr(tools, "_committed_legacy", AsyncMock(return_value=None))
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)

    assert await tools.execute(DEVICE, _send_request(sid, "room:r", "hello", request_id)) == denied
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_send_retry_reuses_its_pinned_room(monkeypatch):
    sid, request_id, pinned_room = uuid.uuid4(), uuid.uuid4(), f"room:{uuid.uuid4()}"
    claim = {"pk": "pk", "sk": "sk", "destination": "person:user_b", "resolvedDestination": pinned_room}
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(tools, "_claim", AsyncMock(return_value=claim))
    resolve = AsyncMock(return_value=f"room:{uuid.uuid4()}")  # what resolution would say now
    monkeypatch.setattr(tools, "_resolve_destination", resolve)
    applied = {"state": "applied", "destination": pinned_room, "messageId": "m"}
    dispatch = AsyncMock(return_value=applied)
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)
    monkeypatch.setattr(tools, "_finish", AsyncMock(side_effect=lambda *args: args[-1]))

    assert await tools.execute(DEVICE, _send_request(sid, "person:user_b", "hello", request_id)) == applied
    assert dispatch.await_args.args[2] == pinned_room
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_person_send_pins_the_resolved_room_before_posting(monkeypatch):
    sid, request_id, room = uuid.uuid4(), uuid.uuid4(), f"room:{uuid.uuid4()}"
    claim = {"pk": "pk", "sk": "sk", "destination": "person:user_b"}
    monkeypatch.setattr(tools, "_source", AsyncMock(return_value={"ownerId": "org_a", "sessionId": str(sid)}))
    monkeypatch.setattr(tools, "_claim", AsyncMock(return_value=claim))
    monkeypatch.setattr(tools, "_resolve_destination", AsyncMock(return_value=room))
    monkeypatch.setattr(tools.messaging_repo, "_table", lambda: SimpleNamespace(update_item=None))
    pin = AsyncMock(return_value={"Attributes": {"resolvedDestination": room}})
    monkeypatch.setattr(tools, "run_in_thread", pin)
    dispatch = AsyncMock(return_value={"state": "applied", "destination": room, "messageId": "m"})
    monkeypatch.setattr(tools, "_dispatch_native", dispatch)
    monkeypatch.setattr(tools, "_finish", AsyncMock(side_effect=lambda *args: args[-1]))

    await tools.execute(DEVICE, _send_request(sid, "person:user_b", "hello", request_id))
    assert "if_not_exists(resolvedDestination" in pin.await_args.kwargs["UpdateExpression"]
    assert dispatch.await_args.args[2] == room


def _decision_device():
    return MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )


def _decision_row(**over):
    return {
        "ownerId": "org_a",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "generation": "g1",
        "connectionId": "connection_a",
        **over,
    }


def _connected(monkeypatch, device_id="mac_a"):
    from core.services import local_session_relay

    relay = SimpleNamespace(connect=AsyncMock(return_value=({"device_id": device_id, "user_id": "user_a"}, None)))
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)


_SID = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.parametrize(
    "row",
    [
        None,
        _decision_row(humanUserId="user_b"),
        _decision_row(deviceId="mac_b"),
        _decision_row(generation="g0"),
        _decision_row(endedAt=1),
    ],
)
async def test_decision_refuses_anything_but_this_macs_current_session(monkeypatch, row):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    _connected(monkeypatch)
    body = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


async def test_decision_refuses_a_stale_connection(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch, device_id="mac_other")
    body = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


async def test_allow_and_pause_run_as_the_owner(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch)
    decide_join = AsyncMock(return_value="joined")
    set_sharing = AsyncMock(return_value="paused")
    monkeypatch.setattr(tools.local_sessions, "decide_join", decide_join)
    monkeypatch.setattr(tools.local_sessions, "set_sharing", set_sharing)
    from core.services import local_session_relay

    publish = AsyncMock()
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", publish)
    allow = tools.NativeDecisionRequest(
        session_id=_SID, generation="g1", decision="allow", user_id="user_z", request_id="r1"
    )
    assert await tools.decide(_decision_device(), allow) == "joined"
    auth = decide_join.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_a", "org_a")
    assert decide_join.await_args.args[1:4] == (str(_SID), "user_z", "allow")
    assert decide_join.await_args.kwargs == {"request_id": "r1"}
    pause = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    assert await tools.decide(_decision_device(), pause) == "paused"
    assert set_sharing.await_args.args[1:] == (str(_SID), False)
    assert set_sharing.await_args.kwargs == {"generation": "g1"}
    publish.assert_awaited_once_with("org_a", "user_a")


@pytest.mark.parametrize(
    "fields",
    [
        {"decision": "allow"},
        {"decision": "decline", "user_id": "user_z"},
        {"decision": "pause", "user_id": "user_z", "request_id": "r1"},
    ],
)
def test_decision_body_names_a_request_exactly_when_it_answers_one(fields):
    with pytest.raises(ValueError):
        tools.NativeDecisionRequest(session_id=uuid.uuid4(), generation="g1", **fields)


@pytest.mark.asyncio
async def test_set_sharing_refuses_a_run_the_caller_did_not_see(monkeypatch):
    sessions = tools.local_sessions
    row = {"sessionId": str(_SID), "humanUserId": "user_a", "generation": "g2"}
    monkeypatch.setattr(sessions, "authorize_session", AsyncMock(return_value=(row, {})))
    put = AsyncMock()
    monkeypatch.setattr(sessions.repo, "put_session", put)
    with pytest.raises(ApiProblem):
        await sessions.set_sharing(
            tools.AuthContext(user_id="user_a", org_id="org_a"), str(_SID), False, generation="g1"
        )
    put.assert_not_awaited()


@pytest.mark.parametrize(
    ("request_id", "routed"),
    [("rp", "pickup"), ("rj", "join"), ("unknown", "join")],
)
async def test_the_mac_answers_each_request_by_its_own_id(monkeypatch, request_id, routed):
    """One person with a join and a pick-up request pending; an older desktop sends no kind."""
    row = _decision_row(
        joinRequests={"user_z": {"state": "pending", "requestId": "rj"}},
        pickupRequests={"user_z": {"state": "pending", "requestId": "rp"}},
    )
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    _connected(monkeypatch)
    doors = {"join": AsyncMock(return_value="join"), "pickup": AsyncMock(return_value="pickup")}
    monkeypatch.setattr(tools.local_sessions, "decide_join", doors["join"])
    monkeypatch.setattr(tools.local_sessions, "decide_pickup_request", doors["pickup"])
    body = tools.NativeDecisionRequest(
        session_id=_SID, generation="g1", decision="allow", user_id="user_z", request_id=request_id
    )
    assert await tools.decide(_decision_device(), body) == routed
    door = doors[routed]
    assert door.await_args.args[1:] == (str(_SID), "user_z", "allow")
    assert door.await_args.kwargs == {"request_id": request_id}
    assert not doors["pickup" if routed == "join" else "join"].await_count


@pytest.mark.asyncio
async def test_person_dm_opens_a_dm_per_send_and_reuses_only_a_real_dm(monkeypatch):
    auth = tools.AuthContext(user_id="user_owner", org_id="org_a")
    person = SimpleNamespace(id="user_b")
    group = SimpleNamespace(room_id=uuid.uuid4(), kind="group-dm", members=[person, SimpleNamespace(id="user_c")])
    rooms = [group]  # the first DM with user_b was converted in place into this group DM
    monkeypatch.setattr(tools.messaging, "list_rooms", AsyncMock(side_effect=lambda _a: SimpleNamespace(rooms=rooms)))
    new_dm = uuid.uuid4()
    create = AsyncMock(return_value=SimpleNamespace(room_id=new_dm))
    monkeypatch.setattr(tools.messaging, "create_room", create)
    monkeypatch.setattr(tools.messaging, "load_agent_roster", AsyncMock(side_effect=RuntimeError("no roster read")))
    with pytest.raises(ApiProblem) as error:
        await tools.person_dm(auth, "user_b")
    assert error.value.error.code == "ROOM_NOT_FOUND"

    # A new person send opens a NEW dm; it never lands in the group.
    assert await tools.person_dm(auth, "user_b", create_key="req-1") == str(new_dm)
    await tools.person_dm(auth, "user_b", create_key="req-2")
    first, second = (call.args[1] for call in create.await_args_list)
    assert first.kind == "dm" and [(m.kind, m.id) for m in first.members] == [("human", "user_b")]
    # Per-send create ids: no send can replay an older create receipt (the group's origin).
    assert first.client_room_id != second.client_room_id

    # Reuse comes from the lookup: once a real DM exists, nothing is created.
    rooms.append(SimpleNamespace(room_id=new_dm, kind="dm", members=[person]))
    create.reset_mock()
    assert await tools.person_dm(auth, "user_b", create_key="req-3") == str(new_dm)
    create.assert_not_awaited()

    await tools.person_dm(auth, "0b5e3c1e-agent", create_key="req-4")
    agent = create.await_args.args[1].members[0]
    assert (agent.kind, agent.id, agent.listen_mode) == ("agent", "0b5e3c1e-agent", "active")
    with pytest.raises(ApiProblem):
        await tools.person_dm(auth, "user_owner", create_key="req-5")


@pytest.mark.asyncio
async def test_session_send_admits_then_dispatches_and_survives_a_dispatch_failure(monkeypatch):
    from core.services import local_session_relay

    sid = str(uuid.uuid4())
    target = {"sessionId": sid, "ownerId": "org_a", "generation": "g7"}
    submit = AsyncMock(return_value=SimpleNamespace(state="pending"))
    monkeypatch.setattr(tools.local_sessions, "submit_command", submit)
    relay = SimpleNamespace(dispatch_session=AsyncMock(side_effect=RuntimeError("socket gone")))
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)
    auth = tools.AuthContext(user_id="user_owner", org_id="org_a")
    result = await tools.session_send(auth, target, "hi", "req-1", via_client="Claude Code")
    assert result == {"state": "pending", "destination": f"session:{sid}", "targetRequestId": "req-1"}
    command = submit.await_args.args[3]
    assert (command.kind, command.text, command.expected_generation) == ("send", "hi", "g7")
    assert submit.await_args.kwargs == {"via_client": "Claude Code"}
    relay.dispatch_session.assert_awaited_once_with("org_a", sid)


@pytest.mark.asyncio
async def test_session_send_refuses_when_control_fails_closed_between_authorize_and_admit(monkeypatch):
    import time

    sid = str(uuid.uuid4())
    clean = {
        "sessionId": sid,
        "ownerId": "org_a",
        "humanUserId": "user_owner",
        "generation": "g",
        "status": "idle",
        "onlineUntil": int(time.time()) + 60,
        "capabilities": {"sendMode": "queue", "interrupt": False},
        "capabilitiesVerified": True,
    }
    # A heartbeat flips the row after the caller's authorize; submit_command's own read sees it.
    flipped = {**clean, "controlUnavailableReason": "history-gap"}
    monkeypatch.setattr(tools.local_sessions, "authorize_session", AsyncMock(return_value=(flipped, {})))
    monkeypatch.setattr(tools.local_sessions.repo, "get_command", AsyncMock(return_value=None))
    admit = AsyncMock()
    monkeypatch.setattr(tools.local_sessions.repo, "admit_command", admit)
    with pytest.raises(ApiProblem) as error:
        await tools.session_send(tools.AuthContext(user_id="user_owner", org_id="org_a"), clean, "hi", "r1")
    assert error.value.error.code == "LOCAL_SESSION_UNSUPPORTED"
    admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_hosted_client_send_never_enters_an_offline_sessions_queue(monkeypatch):
    import time

    sid = str(uuid.uuid4())
    offline = {
        "sessionId": sid,
        "ownerId": "org_a",
        "humanUserId": "user_owner",
        "generation": "g",
        "status": "offline",
        "onlineUntil": int(time.time()) - 5,
        "capabilities": {"sendMode": "queue", "interrupt": False},
        "capabilitiesVerified": True,
    }
    monkeypatch.setattr(tools.local_sessions, "authorize_session", AsyncMock(return_value=(offline, {})))
    monkeypatch.setattr(tools.local_sessions.repo, "get_command", AsyncMock(return_value=None))
    admit = AsyncMock()
    monkeypatch.setattr(tools.local_sessions.repo, "admit_command", admit)
    with pytest.raises(ApiProblem) as error:
        await tools.session_send(
            tools.AuthContext(user_id="user_owner", org_id="org_a"), offline, "hi", "r1", via_client="Claude Code"
        )
    assert error.value.error.code == "LOCAL_SESSION_OFFLINE"
    admit.assert_not_awaited()
