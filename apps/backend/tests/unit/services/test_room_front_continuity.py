"""Changing the default must not move a conversation's voice or erase its reserved seat."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo as repo
from core.services import agent_registry, meeting_invites, messaging
from core.services import task_conversation as tc
from routers import voice_meet
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
