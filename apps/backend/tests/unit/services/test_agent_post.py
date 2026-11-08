"""An agent posting OUTSIDE a room turn (``lightbulb-post.sh``): target resolution, then the
canonical agent writer. Order: roomId > userId/handle > #general."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.services import messaging
from tests.unit.services.test_agent_message_attachments import AGENT, OWNER, ROOM, created  # noqa: F401

DM = str(uuid.UUID(int=77))
GENERAL = messaging.default_room_id(OWNER)
USER = "user_ann"


def _post(**over) -> messaging.AgentPostRequest:
    return messaging.AgentPostRequest.model_validate({"eventId": "post:x:1", "agentId": AGENT, "text": "hi", **over})


def _room_of(create) -> str:
    return create.call_args.kwargs["room_id"]


@pytest.fixture
def dms(monkeypatch):
    """No DM yet; ``create_room`` mints DM. Returns (list_rooms_for_member, create_room) mocks."""
    listed = AsyncMock(return_value=[])
    create_room = AsyncMock(return_value=SimpleNamespace(room_id=uuid.UUID(DM)))
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", listed)
    monkeypatch.setattr(messaging, "create_room", create_room)
    monkeypatch.setattr(messaging, "_validate_human_in_owner", AsyncMock())
    return listed, create_room


@pytest.mark.asyncio
async def test_step1_named_room_passes_straight_through(created):  # noqa: F811
    await messaging.post_agent_message(OWNER, _post(roomId=ROOM))
    assert _room_of(created) == ROOM


@pytest.mark.asyncio
async def test_step1_named_room_the_agent_is_not_in_is_refused_not_joined(created, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(roomId=ROOM))
    assert exc.value.error.code == "MEMBER_NOT_FOUND"
    created.assert_not_called()


@pytest.mark.asyncio
async def test_step1_channel_name_resolves_among_the_agents_channels(created, dms):  # noqa: F811
    listed, _ = dms
    listed.return_value = [
        {"meta": {"roomId": DM, "kind": "dm", "name": None}},
        {"meta": {"roomId": ROOM, "kind": "channel", "name": "Sales"}},
    ]
    await messaging.post_agent_message(OWNER, _post(roomName="#sales"))
    assert _room_of(created) == ROOM
    listed.assert_awaited_once_with(OWNER, "agent", AGENT)


@pytest.mark.asyncio
async def test_step1_unknown_channel_name_is_refused(created, dms):  # noqa: F811
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(roomName="#nope"))
    assert exc.value.error.code == "ACTION_UNAVAILABLE"
    created.assert_not_called()


@pytest.mark.asyncio
async def test_step2_user_reuses_the_existing_dm(created, dms):  # noqa: F811
    listed, create_room = dms
    listed.return_value = [
        {"meta": {"roomId": ROOM, "kind": "channel"}},
        {"meta": {"roomId": DM, "kind": "dm"}},
    ]
    await messaging.post_agent_message(OWNER, _post(userId=USER))
    assert _room_of(created) == DM
    listed.assert_awaited_once_with(OWNER, "human", USER)
    create_room.assert_not_called()


@pytest.mark.asyncio
async def test_step2_user_dm_is_created_when_missing_through_create_room(created, dms):  # noqa: F811
    _, create_room = dms
    await messaging.post_agent_message(OWNER, _post(userId=USER))
    assert _room_of(created) == DM
    auth, request = create_room.call_args.args
    assert (auth.user_id, auth.org_id) == (USER, OWNER)
    assert request.kind == "dm" and request.name is None
    assert [(m.kind, m.id, m.listen_mode) for m in request.members] == [("agent", AGENT, "active")]
    # Deterministic, so two racing posts converge on ONE DM.
    await messaging.post_agent_message(OWNER, _post(userId=USER))
    assert create_room.call_args_list[0].args[1].client_room_id == create_room.call_args_list[1].args[1].client_room_id


@pytest.mark.asyncio
async def test_step2_user_outside_the_workspace_is_refused(created, dms, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        messaging, "_validate_human_in_owner", AsyncMock(side_effect=messaging.ApiProblem("MEMBER_NOT_FOUND", "no"))
    )
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(userId="user_stranger"))
    assert exc.value.error.code == "MEMBER_NOT_FOUND"
    created.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [OWNER, "org_other"])
async def test_step2_an_org_id_is_never_a_human(created, dms, user_id):  # noqa: F811
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(userId=user_id))
    assert exc.value.error.code == "MEMBER_NOT_FOUND"
    dms[1].assert_not_called()
    created.assert_not_called()


@pytest.mark.asyncio
async def test_step2_a_dm_the_human_left_is_not_reused(created, dms, monkeypatch):  # noqa: F811
    listed, create_room = dms
    listed.return_value = [{"meta": {"roomId": ROOM, "kind": "dm"}}]
    monkeypatch.setattr(
        messaging.messaging_repo,
        "get_member",
        AsyncMock(side_effect=lambda room, kind, mid: None if kind == "human" else {"kind": kind, "id": mid}),
    )
    await messaging.post_agent_message(OWNER, _post(userId=USER))
    assert _room_of(created) == DM
    create_room.assert_awaited_once()


@pytest.mark.asyncio
async def test_step2_handle_resolves_a_workspace_member(created, dms, monkeypatch):  # noqa: F811
    import core.services.clerk_admin as clerk_admin

    monkeypatch.setattr(
        clerk_admin,
        "list_organization_members",
        AsyncMock(
            return_value=[
                {"user_id": "user_bob", "first_name": "Bob", "email": "bob@x.com"},
                {"user_id": USER, "first_name": "Ann", "email": "ann.lee@x.com"},
            ]
        ),
    )
    await messaging.post_agent_message(OWNER, _post(handle="@ann-lee"))
    assert _room_of(created) == DM
    assert dms[0].call_args.args == (OWNER, "human", USER)
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(handle="nobody"))
    assert exc.value.error.code == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_an_issue_id_is_accepted_and_ignored(created, monkeypatch):  # noqa: F811
    """``lightbulb-post.sh --issue`` keeps working: the id is ignored and the post lands in #general."""
    await messaging.post_agent_message(OWNER, _post(issueId="iss-1"))
    assert _room_of(created) == GENERAL


@pytest.mark.asyncio
async def test_step4_no_target_posts_to_general(created):  # noqa: F811
    await messaging.post_agent_message(OWNER, _post())
    assert _room_of(created) == GENERAL


@pytest.mark.asyncio
async def test_step4_missing_general_is_a_409_not_a_create(created, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=None))
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post())
    assert exc.value.error.code == "ACTION_UNAVAILABLE" and exc.value.status_code == 409
    created.assert_not_called()


@pytest.mark.asyncio
async def test_a_retried_post_replays_with_the_same_idempotency_key_and_hash(created):  # noqa: F811
    await messaging.post_agent_message(OWNER, _post())
    await messaging.post_agent_message(OWNER, _post())
    first, second = (call.kwargs for call in created.call_args_list)
    assert (first["idemp_sk"], first["req_hash"]) == (second["idemp_sk"], second["req_hash"])


def test_at_most_one_of_room_user_handle():
    with pytest.raises(ValidationError):
        _post(roomId=ROOM, userId=USER)
    with pytest.raises(ValidationError):
        _post(text="   ")
    with pytest.raises(ValidationError):
        _post(issueId="../agents/x")
    assert _post(roomId=ROOM, issueId="iss-1").room_id == uuid.UUID(ROOM)
