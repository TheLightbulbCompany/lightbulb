from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging


OWNER = "org_1"
ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"


def _request():
    return messaging.UpsertRoomMemberRequest(
        kind="agent",
        id=AGENT,
        role="member",
        listenMode="active",
    )


def _wire_add(monkeypatch: pytest.MonkeyPatch, *, room_kind: str, role: str = "member") -> AsyncMock:
    meta = {"roomId": ROOM, "ownerId": OWNER, "kind": room_kind, "revision": 4}
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=(meta, {"kind": "human", "id": "user_a", "role": role})),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Atlas"}]))
    monkeypatch.setattr(
        messaging,
        "_resolved_member",
        AsyncMock(
            return_value={
                "kind": "agent",
                "id": AGENT,
                "displayName": "Atlas",
                "mentionHandle": "atlas",
                "role": "member",
                "listenMode": "active",
            }
        ),
    )
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    upsert = AsyncMock(return_value=meta)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_emit_member_added", lambda *args, **kwargs: None)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())
    return upsert


@pytest.mark.asyncio
async def test_any_group_chat_member_may_add_an_existing_workspace_person(monkeypatch):
    upsert = _wire_add(monkeypatch, room_kind="group-dm")

    await messaging.upsert_member(AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request())

    assert upsert.await_args.kwargs["room_kind"] is None


@pytest.mark.asyncio
async def test_adding_to_a_dm_converts_it_to_a_group_chat_in_place(monkeypatch):
    upsert = _wire_add(monkeypatch, room_kind="dm")

    await messaging.upsert_member(AuthContext(user_id="user_a", org_id=OWNER), ROOM, _request())

    assert upsert.await_args.kwargs["room_id"] == ROOM
    assert upsert.await_args.kwargs["room_kind"] == "group-dm"
