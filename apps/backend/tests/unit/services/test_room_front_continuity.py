"""Changing the default must not move a conversation's voice or erase its reserved seat."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import tasks_schema as ts
from core.repositories import agent_registry_repo, automations_repo
from core.repositories import messaging_repo as repo
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import admission, agent_registry, meeting_invites, messaging
from core.services import task_conversation as tc
from core.temporal import activities
from core.temporal.types import RouterTurnIn
from routers import voice_meet
from temporalio.testing import ActivityEnvironment
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401 -- local moto store

OWNER, HUMAN = "org_1", "user_sam"
A, B, C = (str(uuid.UUID(int=i)) for i in (101, 102, 103))


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    roster = [
        {"id": A, "name": "Original", "role": "ceo", "status": "active"},
        {"id": B, "name": "Next", "role": "engineer", "status": "active"},
        {"id": C, "name": "Finance", "role": "finance", "status": "active"},
    ]
    config = {"frontAgentId": A}
    monkeypatch.setattr(
        agent_registry,
        "roster",
        AsyncMock(side_effect=lambda ws: copy.deepcopy([a for a in roster if a["status"] != "deleted"])),
    )
    monkeypatch.setattr(repo, "get_voice_agent", AsyncMock(side_effect=lambda ws: dict(config)))
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row",
        AsyncMock(return_value=SimpleNamespace(starter_agent_ids={"ceo": A})),
    )
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Sam"))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "_publish_message_created_soon", lambda item: None)
    publisher = SimpleNamespace(publish_user=AsyncMock())
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: publisher)
    monkeypatch.setattr("core.services.questions.guard_reply", AsyncMock(return_value=None))
    return SimpleNamespace(roster=roster, config=config, publisher=publisher, table=table)


def _seat(agent_id, handle, **extra):
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": "Old label",
        "mentionHandle": handle,
        "role": "member",
        "listenMode": "mentions",
        **extra,
    }


async def _room(key, *, pinned=True, other_seats=(), owner=OWNER, seat_extra=None, extra=None):
    room_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    return await repo.create_room(
        owner_id=owner,
        room_id=room_id,
        client_room_id=key,
        creator_user_id=HUMAN,
        name="Private",
        kind="channel",
        req_hash=repo.request_hash(key),
        members=[
            {"kind": "human", "id": HUMAN, "displayName": "Sam", "role": "owner", "listenMode": None},
            _seat(A, "edited-a" if pinned else "lightbulb", **(seat_extra or {})),
            *other_seats,
        ],
        extra={**({"frontAgentId": A} if pinned else {}), **(extra or {})},
    )


async def _answer(room_id, event):
    return await tc.post_as_lightbulb(
        OWNER,
        room_id,
        text=f"Answer {event}",
        event_id=f"router:{event}",
        remember=False,
    )


async def _meeting(key):
    room_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    await meeting_invites._create_room(
        OWNER,
        room_id,
        SimpleNamespace(uid=key, summary="Standup"),
        HUMAN,
        {"uid": key, "title": "Standup", "status": "in_call"},
    )
    return room_id


async def test_router_answer_remains_spoken_in_active_meeting_after_default_switch(world):
    """Real meeting seating → Router post → canonical write → voice polling; no identity helpers mocked."""
    general = messaging.default_room_id(OWNER)
    # The output room also has A, so the broken redirect silently succeeds instead of failing a mock.
    await repo.create_room(
        owner_id=OWNER,
        room_id=general,
        client_room_id="general",
        creator_user_id=HUMAN,
        name="general",
        kind="channel",
        req_hash=repo.request_hash("general"),
        members=[_seat(A, "lightbulb")],
        extra={"frontAgentId": A},
    )
    meeting = await _meeting("old-meeting")
    first = await _answer(meeting, "before-switch")
    world.config["frontAgentId"] = B
    second = await _answer(meeting, "after-switch")

    lines = (await voice_meet.agent_lines(meeting, 0))["messages"]
    assert [line["id"] for line in lines] == [first, second]
    assert (await repo.get_room(meeting))["frontAgentId"] == A
    assert (await repo.get_room(general))["lastSeq"] == 0
    assert (await repo.get_message_by_id(meeting, second))["author"] == {
        "kind": "agent",
        "id": A,
        "displayName": messaging.FRONT_AGENT_NAME,
    }

    # The normal creation path takes B for a new meeting; it does not change the old meeting.
    new_meeting = await _meeting("new-meeting")
    new_answer = await _answer(new_meeting, "new-meeting")
    assert (await repo.get_room(new_meeting))["frontAgentId"] == B
    assert (await repo.get_message_by_id(new_meeting, new_answer))["author"]["id"] == B
    assert [line["id"] for line in (await voice_meet.agent_lines(new_meeting, 0))["messages"]] == [new_answer]


async def test_ended_meeting_still_routes_unbound_answer_to_output(world):
    meeting = await _meeting("ended-meeting")
    general = messaging.default_room_id(OWNER)
    await repo.create_room(
        owner_id=OWNER,
        room_id=general,
        client_room_id="general",
        creator_user_id=HUMAN,
        name="general",
        kind="channel",
        req_hash=repo.request_hash("general"),
        members=[_seat(A, "lightbulb")],
        extra={"frontAgentId": A},
    )
    meta = await repo.get_room(meeting)
    await repo.patch_room_meta(
        meeting,
        {"meeting": {**meta["meeting"], "status": "ended"}},
        expected_revision=meta["revision"],
    )
    world.config["frontAgentId"] = B
    answer = await _answer(meeting, "after-meeting")
    assert await repo.get_message_by_id(meeting, answer) is None
    assert (await repo.get_message_by_id(general, answer))["author"]["id"] == A
    assert (await voice_meet.agent_lines(meeting, 0))["messages"] == []


async def test_another_seated_agent_does_not_gain_active_meeting_redirect_exception(world, monkeypatch):
    meeting = await _meeting("nonfront-meeting")
    world.config["frontAgentId"] = B
    output = AsyncMock(return_value=meeting)
    monkeypatch.setattr(meeting_invites, "output_room", output)
    with pytest.raises(messaging.ApiProblem) as refused:
        await messaging.create_agent_message(
            OWNER,
            messaging.AgentMessageRequest(
                event_id="ordinary-worker-answer",
                room_id=meeting,
                agent_id=B,
                text="Worker answer",
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
        )
    assert refused.value.error.code == "ROOM_FORBIDDEN"
    output.assert_awaited_once_with(OWNER, meeting)
    assert (await repo.get_room(meeting))["lastSeq"] == 0


@pytest.mark.parametrize("pinned", [False, True], ids=["legacy", "modern"])
@pytest.mark.parametrize("seat_selected", [False, True], ids=["selected-absent", "selected-seated"])
async def test_rename_refresh_preserves_room_front_and_subsequent_router_post(world, pinned, seat_selected):
    meta = await _room("rename-room", pinned=pinned, other_seats=[_seat(B, "next")] if seat_selected else [])
    room_id = meta["roomId"]
    before_members = await repo.list_members(room_id)
    world.config["frontAgentId"] = B
    world.roster[0]["name"] = "Renamed original"

    old = await messaging.refresh_agent_member_names(OWNER, A, await messaging.load_agent_roster(OWNER))
    assert old == {room_id: "Old label"}
    answer = await _answer(room_id, "after-rename")
    assert (await repo.get_message_by_id(room_id, answer))["author"]["id"] == A
    member = await repo.get_member(room_id, "agent", A)
    assert (member["displayName"], member["mentionHandle"]) == (messaging.FRONT_AGENT_NAME, "lightbulb")
    # Renaming the new global choice must not stamp a second reserved seat into this old room.
    if seat_selected:
        await messaging.refresh_agent_member_names(OWNER, B)
        assert (await repo.get_member(room_id, "agent", B))["mentionHandle"] != "lightbulb"
        assert await tc.front_agent_id(OWNER, room_id=room_id) == A
    after_members = await repo.list_members(room_id)

    def strip_names(members):
        return [{k: v for k, v in m.items() if k not in {"displayName", "mentionHandle"}} for m in members]

    assert strip_names(after_members) == strip_names(before_members)
    assert (await repo.get_room(room_id)).get("frontAgentId") == (A if pinned else None)


async def test_ordinary_nonfront_rename_still_refreshes_name_handle_and_people(world):
    meta = await _room("nonfront-rename", other_seats=[_seat(C, "finance")])
    world.config["frontAgentId"] = B
    world.roster[2]["name"] = "New finance"
    front_before = await repo.get_member(meta["roomId"], "agent", A)
    assert await messaging.refresh_agent_member_names(OWNER, C) == {meta["roomId"]: "Old label"}
    member = await repo.get_member(meta["roomId"], "agent", C)
    assert (member["displayName"], member["mentionHandle"]) == ("New finance", "new-finance")
    assert await repo.get_member(meta["roomId"], "agent", A) == front_before
    world.publisher.publish_user.assert_awaited_once()
    assert world.publisher.publish_user.await_args.args[:2] == (OWNER, HUMAN)


@pytest.mark.parametrize("pinned", [False, True], ids=["legacy", "modern"])
@pytest.mark.parametrize("status", ["paused", "deleted"])
async def test_unavailable_front_rename_cannot_retarget_to_seated_global_choice(world, pinned, status):
    meta = await _room("inactive-front", pinned=pinned, other_seats=[_seat(B, "next")])
    world.config["frontAgentId"] = B
    world.roster[0].update(status=status, name="Unavailable rename")
    with pytest.raises(tc.NoFrontAgent):
        await _answer(meta["roomId"], "before-invalid-rename")
    await messaging.refresh_agent_member_names(OWNER, A)
    with pytest.raises(tc.NoFrontAgent):
        await _answer(meta["roomId"], "after-invalid-rename")
    assert (await repo.get_room(meta["roomId"]))["lastSeq"] == 0


async def test_rename_preserves_conflicting_legacy_seats_as_a_refusal(world):
    meta = await _room("conflicting-fronts", pinned=False, other_seats=[_seat(B, "lightbulb")])
    world.config["frontAgentId"] = B
    before = await repo.list_members(meta["roomId"])
    await messaging.refresh_agent_member_names(OWNER, A)
    assert await repo.list_members(meta["roomId"]) == before
    with pytest.raises(tc.NoFrontAgent, match="conflicting"):
        await _answer(meta["roomId"], "conflicting")


@pytest.mark.parametrize("change", ["removed", "foreign-home", "nonordinary-role"])
async def test_stale_member_index_cannot_refresh_or_recreate_a_changed_seat(world, monkeypatch, change):
    meta = await _room("stale-member", pinned=False)
    stale = await repo.list_rooms_for_member(OWNER, "agent", A)
    key = {"pk": repo.room_pk(meta["roomId"]), "sk": repo.member_sk("agent", A)}
    if change == "removed":
        world.table.delete_item(Key=key)
    else:
        world.table.update_item(
            Key=key,
            UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": "homeOwnerId" if change == "foreign-home" else "role"},
            ExpressionAttributeValues={":value": "org_other" if change == "foreign-home" else "owner"},
        )
    before = await repo.get_member(meta["roomId"], "agent", A)
    monkeypatch.setattr(repo, "list_rooms_for_member", AsyncMock(return_value=stale))
    assert await messaging.refresh_agent_member_names(OWNER, A) == {}
    assert await repo.get_member(meta["roomId"], "agent", A) == before
    world.publisher.publish_user.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["wrong-owner", "foreign-seat", "deleted-room", "nonordinary-seat"])
async def test_rename_never_changes_or_authorizes_invalid_room_seats(world, invalid):
    meta = await _room(
        "invalid-front",
        pinned=False,
        owner="org_other" if invalid == "wrong-owner" else OWNER,
        seat_extra={"homeOwnerId": OWNER if invalid == "wrong-owner" else "org_other"}
        if invalid in {"wrong-owner", "foreign-seat"}
        else ({"role": "owner"} if invalid == "nonordinary-seat" else {}),
        extra={"deletedAt": "2026-11-09T00:00:00Z"} if invalid == "deleted-room" else {},
    )
    world.config["frontAgentId"] = B
    world.roster[0]["name"] = "Untrusted rename"
    before = await repo.get_member(meta["roomId"], "agent", A)
    await messaging.refresh_agent_member_names(OWNER, A)
    assert await repo.get_member(meta["roomId"], "agent", A) == before
    with pytest.raises(tc.NoFrontAgent):
        await _answer(meta["roomId"], "invalid-seat")
    assert (await repo.get_room(meta["roomId"]))["lastSeq"] == 0


async def _human_message(room_id, key):
    msg, _ = await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.room_pk(room_id),
        idemp_sk=repo.human_idemp_sk(HUMAN, key),
        req_hash=repo.request_hash(key),
        author={"kind": "human", "id": HUMAN, "displayName": "Sam"},
        text="The launch date is Tuesday",
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=None,
        client_message_id=key,
        acting_human_id=HUMAN,
    )
    return msg


@pytest.mark.parametrize("pinned", [False, True], ids=["legacy", "modern"])
async def test_quiet_read_receipt_uses_existing_room_front_after_default_switch(world, monkeypatch, pinned):
    """RoomPanel clears its reading indicator only for the room front's UUID, never the new default."""
    meta = await _room("quiet-old-room", pinned=pinned)
    room_id = meta["roomId"]
    before = await repo.list_members(room_id)
    world.config["frontAgentId"] = B
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    repo.get_voice_agent.reset_mock()
    owner_row = WorkspaceRepo.resolve_owner_row
    owner_row.reset_mock()

    await tc.read_quietly(OWNER, {"roomId": room_id, "messageId": "quiet-message"})

    publish.assert_awaited_once()
    published_room, event = publish.await_args.args
    assert published_room == room_id
    assert event["type"] == "room.read.changed"
    assert event["payload"] == {
        "roomId": room_id,
        "reader": {"kind": "agent", "id": A},
        "messageId": "quiet-message",
    }
    repo.get_voice_agent.assert_not_awaited()
    owner_row.assert_not_awaited()
    assert await repo.list_members(room_id) == before
    assert await repo.get_member(room_id, "agent", B) is None


async def test_quiet_read_receipt_uses_new_room_selected_front(world, monkeypatch):
    world.config["frontAgentId"] = B
    room_id = await tc.ensure_task_room(
        OWNER, {"taskId": str(uuid.UUID(int=900)), "title": "New task", "audience": {"kind": "workspace"}}
    )
    assert (await repo.get_room(room_id))["frontAgentId"] == B
    before = await repo.list_members(room_id)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    repo.get_voice_agent.reset_mock()

    await tc.read_quietly(OWNER, {"roomId": room_id, "messageId": "new-message"})

    publish.assert_awaited_once()
    assert publish.await_args.args[0] == room_id
    assert publish.await_args.args[1]["payload"] == {
        "roomId": room_id,
        "reader": {"kind": "agent", "id": B},
        "messageId": "new-message",
    }
    repo.get_voice_agent.assert_not_awaited()
    assert await repo.list_members(room_id) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "removed",
        "foreign-seat",
        "nonordinary-seat",
        "paused-agent",
        "deleted-agent",
        "wrong-owner",
        "deleted-room",
        "missing-room",
    ],
)
async def test_quiet_read_receipt_fails_closed_without_authorized_active_room_front(world, monkeypatch, invalid):
    meta = await _room(
        "quiet-invalid-room",
        other_seats=[_seat(B, "next")],  # a seated global choice cannot replace an invalid pin
        owner="org_other" if invalid == "wrong-owner" else OWNER,
        seat_extra={"homeOwnerId": "org_other"}
        if invalid == "foreign-seat"
        else ({"role": "owner"} if invalid == "nonordinary-seat" else {}),
        extra={"deletedAt": "2026-11-09T00:00:00Z"} if invalid == "deleted-room" else {},
    )
    room_id = meta["roomId"]
    if invalid == "removed":
        world.table.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("agent", A)})
    elif invalid in {"paused-agent", "deleted-agent"}:
        world.roster[0]["status"] = "paused" if invalid == "paused-agent" else "deleted"
    world.config["frontAgentId"] = B
    before = await repo.list_members(room_id)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    repo.get_voice_agent.reset_mock()

    await tc.read_quietly(
        OWNER,
        {"roomId": str(uuid.UUID(int=999)) if invalid == "missing-room" else room_id, "messageId": "quiet-message"},
    )

    publish.assert_not_awaited()
    repo.get_voice_agent.assert_not_awaited()
    assert await repo.list_members(room_id) == before


async def test_actual_admission_receipt_retains_existing_room_front(world, monkeypatch):
    meta = await _room("independent-review-receipt", seat_extra={"listenMode": "active"})
    room_id = meta["roomId"]
    await repo.set_agent_names(room_id, A, "Lightbulb", "lightbulb")
    world.config["frontAgentId"] = B
    assert await tc.front_agent_id(OWNER, room_id=room_id) == A
    assert await tc.front_agent_id(OWNER) == B
    assert await repo.get_member(room_id, "agent", B) is None
    assert await admission.router_room(meta, OWNER)
    monkeypatch.setattr(admission.room_memory, "ingest_message", AsyncMock())
    monkeypatch.setattr(
        agent_registry_repo, "list_agents", AsyncMock(return_value=[{"agentId": a["id"], **a} for a in world.roster])
    )
    monkeypatch.setattr(automations_repo, "list_rules", AsyncMock(return_value=[]))
    jev = AsyncMock(return_value={"to": "none"})
    monkeypatch.setattr(admission.jev, "route_for_router", jev)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    msg = await _human_message(room_id, "review-message")
    await admission.admit_pointer({"kind": "admit", "roomId": room_id, "throughSeq": msg["messageSeq"]})
    jev.assert_awaited_once()
    publish.assert_awaited_once()
    event = publish.await_args.args[1]
    assert event["payload"]["reader"] == {"kind": "agent", "id": A}
    assert await repo.get_member(room_id, "agent", B) is None


@pytest.mark.parametrize("switch_default", [False, True], ids=["control-A", "switched-B"])
async def test_actual_router_note_reaction_retains_existing_room_front(world, monkeypatch, switch_default):
    meta = await _room("independent-review-reaction", seat_extra={"listenMode": "active"})
    room_id = meta["roomId"]
    world.config["frontAgentId"] = B if switch_default else A
    assert await tc.front_agent_id(OWNER, room_id=room_id) == A
    assert await repo.get_member(room_id, "agent", B) is None
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    msg = await _human_message(room_id, "review-reaction")
    tid = ts.trigger_id("chat", msg["messageId"])
    trig = {
        "triggerId": tid,
        "source": "chat",
        "admission": "candidate",
        "ref": {"roomId": room_id, "messageId": msg["messageId"]},
    }
    repo.get_voice_agent.reset_mock()
    await ActivityEnvironment().run(
        activities._apply,
        OWNER,
        RouterTurnIn(OWNER, "shared", "review-batch", [tid]),
        {tid: trig},
        [ts.Note(kind="note", trigger_ids=[tid], fact="Launch date is Tuesday")],
        None,
    )
    saved = await repo.get_message_by_id(room_id, msg["messageId"])
    repo.get_voice_agent.assert_not_awaited()
    assert saved.get("reactions", {}).get("✅") == [A]
    reaction_events = [
        call.args for call in publish.await_args_list if call.args[1]["type"] == "room.message.reactions.changed"
    ]
    assert len(reaction_events) == 1
    assert reaction_events[0][0] == room_id
    assert reaction_events[0][1]["payload"]["reactions"] == {"✅": [A]}
    assert await repo.get_member(room_id, "agent", B) is None


@pytest.mark.parametrize(
    "invalid",
    ["removed", "foreign-seat", "nonordinary-seat", "paused-agent", "deleted-agent", "wrong-owner", "deleted-room"],
)
async def test_router_reaction_fails_closed_without_authorized_active_room_front(world, monkeypatch, invalid):
    meta = await _room("reaction-invalid-room", other_seats=[_seat(B, "next")])
    room_id = meta["roomId"]
    msg = await _human_message(room_id, "invalid-reaction")
    if invalid == "removed":
        world.table.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("agent", A)})
    elif invalid in {"paused-agent", "deleted-agent"}:
        world.roster[0]["status"] = "paused" if invalid == "paused-agent" else "deleted"
    else:
        field, value = {
            "foreign-seat": ("homeOwnerId", "org_other"),
            "nonordinary-seat": ("role", "owner"),
            "wrong-owner": ("ownerId", "org_other"),
            "deleted-room": ("deletedAt", "2026-11-09T00:00:00Z"),
        }[invalid]
        sk = repo.member_sk("agent", A) if invalid in {"foreign-seat", "nonordinary-seat"} else "META"
        world.table.update_item(
            Key={"pk": repo.room_pk(room_id), "sk": sk},
            UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value},
        )
    world.config["frontAgentId"] = B
    before = await repo.list_members(room_id)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    repo.get_voice_agent.reset_mock()

    await activities._react(OWNER, {"roomId": room_id, "messageId": msg["messageId"]}, "✅")

    assert not (await repo.get_message_by_id(room_id, msg["messageId"])).get("reactions")
    publish.assert_not_awaited()
    repo.get_voice_agent.assert_not_awaited()
    assert await repo.list_members(room_id) == before


async def test_router_reactions_use_new_room_selected_front(world, monkeypatch):
    world.config["frontAgentId"] = B
    room_id = await tc.ensure_task_room(
        OWNER,
        {"taskId": str(uuid.UUID(int=901)), "title": "New task", "audience": {"kind": "person", "userIds": [HUMAN]}},
    )
    msg = await _human_message(room_id, "new-reaction")
    before = await repo.list_members(room_id)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_event", publish)
    repo.get_voice_agent.reset_mock()

    await activities._react_eyes(OWNER, {"roomId": room_id, "messageId": msg["messageId"]})
    await activities._react(OWNER, {"roomId": room_id, "messageId": msg["messageId"]}, "✅")

    assert (await repo.get_message_by_id(room_id, msg["messageId"]))["reactions"] == {"👀": [B], "✅": [B]}
    assert publish.await_count == 2
    assert all(call.args[0] == room_id for call in publish.await_args_list)
    repo.get_voice_agent.assert_not_awaited()
    assert await repo.list_members(room_id) == before


@pytest.mark.parametrize("pinned", [False, True], ids=["legacy", "pinned"])
@pytest.mark.parametrize("selected_seated", [False, True], ids=["selected-absent", "selected-seated"])
@pytest.mark.parametrize("changed", [A, C], ids=["front-mode", "other-mode"])
async def test_member_mode_update_preserves_conversation_front(world, monkeypatch, pinned, selected_seated, changed):
    from core.auth import AuthContext

    meta = await _room(
        "member-mode-front",
        pinned=pinned,
        other_seats=[_seat(C, "finance"), *([_seat(B, "next")] if selected_seated else [])],
    )
    room_id = meta["roomId"]
    before = await repo.list_members(room_id)
    world.config["frontAgentId"] = B
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value=meta))

    await messaging.upsert_member(
        AuthContext(user_id=HUMAN, org_id=OWNER),
        room_id,
        messaging.UpsertRoomMemberRequest(kind="agent", id=changed, role="member", listen_mode="active"),
    )

    assert await tc.front_agent_id(OWNER, room_id=room_id) == A
    assert (await repo.get_member(room_id, "agent", A))["mentionHandle"] == "lightbulb"
    assert (await repo.get_member(room_id, "agent", changed))["listenMode"] == "active"
    after = await repo.list_members(room_id)
    assert {(m["kind"], m["id"]) for m in after} == {(m["kind"], m["id"]) for m in before}
    if selected_seated:
        assert (await repo.get_member(room_id, "agent", B))["mentionHandle"] != "lightbulb"
    else:
        assert await repo.get_member(room_id, "agent", B) is None
    answer = await _answer(room_id, "after-mode-update")
    assert (await repo.get_message_by_id(room_id, answer))["author"]["id"] == A


@pytest.mark.parametrize("invalid", ["paused", "deleted", "conflicting"])
async def test_member_mode_update_cannot_erase_unavailable_legacy_front(world, monkeypatch, invalid):
    from core.auth import AuthContext

    meta = await _room(
        "invalid-member-mode-front",
        pinned=False,
        other_seats=[_seat(B, "lightbulb" if invalid == "conflicting" else "next"), _seat(C, "finance")],
    )
    world.config["frontAgentId"] = B
    if invalid != "conflicting":
        world.roster[0]["status"] = invalid
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value=meta))
    with pytest.raises(tc.NoFrontAgent):
        await tc.front_agent_id(OWNER, room_id=meta["roomId"])
    await messaging.upsert_member(
        AuthContext(user_id=HUMAN, org_id=OWNER),
        meta["roomId"],
        messaging.UpsertRoomMemberRequest(kind="agent", id=C, role="member", listen_mode="active"),
    )
    assert (await repo.get_member(meta["roomId"], "agent", A))["mentionHandle"] == "lightbulb"
    with pytest.raises(tc.NoFrontAgent):
        await tc.front_agent_id(OWNER, room_id=meta["roomId"])
