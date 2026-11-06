from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging


OWNER = "org_1"
ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"


def _request(*, kind: str = "agent", member_id: str = AGENT, role: str = "member"):
    return messaging.UpsertRoomMemberRequest(
        kind=kind,
        id=member_id,
        role=role,
        listenMode="active" if kind == "agent" else None,
    )


def _wire_add(
    monkeypatch: pytest.MonkeyPatch,
    *,
    room_kind: str,
    actor_role: str = "member",
    target_kind: str = "agent",
    target_id: str = AGENT,
    target_role: str = "member",
) -> tuple[AsyncMock, AsyncMock]:
    meta = {"roomId": ROOM, "ownerId": OWNER, "kind": room_kind, "revision": 4}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "user_a", "role": actor_role})),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Atlas"}]))
    monkeypatch.setattr(
        messaging,
        "_resolved_member",
        AsyncMock(
            return_value={
                "kind": target_kind,
                "id": target_id,
                "displayName": "Atlas" if target_kind == "agent" else "New person",
                "mentionHandle": "atlas" if target_kind == "agent" else "new-person",
                "role": target_role,
                "listenMode": "active" if target_kind == "agent" else None,
            }
        ),
    )
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="stream-key"))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    upsert = AsyncMock(return_value=meta)
    publish_user = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_user": publish_user})())
    monkeypatch.setattr(messaging, "_emit_member_added", lambda *args, **kwargs: None)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())
    return upsert, publish_user


@pytest.mark.asyncio
async def test_any_group_chat_member_may_add_an_existing_workspace_person(monkeypatch):
    upsert, _ = _wire_add(monkeypatch, room_kind="group-dm")

    await messaging.upsert_member(AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request())

    assert upsert.await_args.kwargs["room_id"] == ROOM


@pytest.mark.asyncio
async def test_adding_to_a_dm_starts_a_new_group_chat_and_leaves_the_dm_alone(monkeypatch):
    """Founder ruling (7 Nov 2026): like Slack, the DM stays private; the new chat holds everyone plus the newcomer."""
    upsert, _ = _wire_add(monkeypatch, room_kind="dm")
    lightbulb = {"kind": "agent", "id": AGENT, "role": "member", "listenMode": "active"}
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_members",
        AsyncMock(return_value=[{"kind": "human", "id": "user_a", "role": "owner", "listenMode": None}, lightbulb]),
    )
    create = AsyncMock(return_value="new group chat")
    monkeypatch.setattr(messaging, "create_room", create)
    auth = AuthContext(user_id="user_a", org_id=OWNER)

    detail = await messaging.upsert_member(auth, ROOM, _request(kind="human", member_id="user_b"))

    assert detail == "new group chat"
    upsert.assert_not_awaited()  # the DM gains nobody: the newcomer never reads it
    request = create.await_args.args[1]
    assert request.kind == "group-dm" and request.name is None
    assert {(m.kind, m.id) for m in request.members} == {("agent", AGENT), ("human", "user_b")}
    # A retry (or the same add later) lands on the same chat; a different person makes a different one.
    await messaging.upsert_member(auth, ROOM, _request(kind="human", member_id="user_b"))
    assert create.await_args.args[1].client_room_id == request.client_room_id
    await messaging.upsert_member(auth, ROOM, _request(kind="human", member_id="user_c"))
    assert create.await_args.args[1].client_room_id != request.client_room_id


@pytest.mark.asyncio
async def test_group_chat_rejects_admin_role_even_from_owner(monkeypatch):
    upsert, _ = _wire_add(
        monkeypatch,
        room_kind="group-dm",
        actor_role="owner",
        target_kind="human",
        target_id="user_b",
        target_role="admin",
    )

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(
            AuthContext(user_id="user_a", org_id=OWNER),
            ROOM,
            _request(kind="human", member_id="user_b", role="admin"),
        )

    assert exc.value.error.code == "INVALID_REQUEST"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_human_receives_room_list_invalidation(monkeypatch):
    _, publish_user = _wire_add(
        monkeypatch,
        room_kind="group-dm",
        target_kind="human",
        target_id="user_b",
    )

    await messaging.upsert_member(
        AuthContext(user_id="user_a", org_id=OWNER),
        ROOM,
        _request(kind="human", member_id="user_b"),
    )

    publish_user.assert_awaited_once()
    owner_arg, user_arg, event = publish_user.await_args.args
    assert (owner_arg, user_arg) == (OWNER, "user_b")
    assert event["type"] == "room.member.updated"
    assert event["payload"] == {"roomId": ROOM}


@pytest.mark.asyncio
async def test_existing_humans_receive_room_list_invalidation(monkeypatch):
    _, publish_user = _wire_add(monkeypatch, room_kind="group-dm")
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_members",
        AsyncMock(
            return_value=[
                {"kind": "human", "id": "user_a", "role": "owner"},
                {"kind": "human", "id": "user_c", "role": "member"},
                {"kind": "agent", "id": "agent_x", "role": "member"},
            ]
        ),
    )

    await messaging.upsert_member(AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request())

    notified = {call.args[1] for call in publish_user.await_args_list}
    assert notified == {"user_a", "user_c"}


@pytest.mark.asyncio
async def test_one_failed_invalidation_does_not_skip_the_rest(monkeypatch):
    _, publish_user = _wire_add(monkeypatch, room_kind="group-dm")
    monkeypatch.setattr(
        messaging.messaging_repo,
        "list_members",
        AsyncMock(
            return_value=[
                {"kind": "human", "id": "user_a", "role": "owner"},
                {"kind": "human", "id": "user_c", "role": "member"},
            ]
        ),
    )

    def fail_for_user_a(owner, user, event):
        if user == "user_a":
            raise RuntimeError("appsync down")
        return True

    publish_user.side_effect = fail_for_user_a
    publish_metadata = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish_metadata)

    await messaging.upsert_member(AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request())

    assert {call.args[1] for call in publish_user.await_args_list} == {"user_a", "user_c"}
    publish_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrying_an_addition_that_already_committed_is_allowed(monkeypatch):
    upsert, publish_user = _wire_add(monkeypatch, room_kind="group-dm", target_kind="human", target_id="user_b")
    monkeypatch.setattr(
        messaging.messaging_repo,
        "get_member",
        AsyncMock(return_value={"kind": "human", "id": "user_b", "role": "member"}),
    )

    await messaging.upsert_member(
        AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request(kind="human", member_id="user_b")
    )

    upsert.assert_awaited_once()
    publish_user.assert_not_awaited()
