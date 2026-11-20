"""A workspace removal unseats the person from every room they sit in. Voice reads room membership as its
authorization (the talk orb's DM, owner phone mode, a task's room), so a removed member must lose every seat."""

import asyncio
import uuid

import pytest

from core.repositories import messaging_repo as repo
from core.services import messaging
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

OWNER = "org_removal"
AGENT = "10000000-0000-4000-8000-000000000001"


def _human(user_id: str, role: str = "member") -> dict:
    return {"kind": "human", "id": user_id, "role": role, "listenMode": None, "displayName": user_id}


async def _room(room_id: str, kind: str, members: list[dict]) -> None:
    await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=room_id,
        creator_user_id="user_A",
        name=None if kind != "channel" else room_id,
        kind=kind,
        members=members,
        req_hash=repo.request_hash(room_id, kind, members),
    )


@pytest.mark.asyncio
async def test_removed_member_loses_room_access(table, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging, "human_display_name", lambda user: asyncio.sleep(0, result=user))
    agent = {"kind": "agent", "id": AGENT, "role": "member", "listenMode": "active", "displayName": "Lightbulb"}
    rooms = {
        messaging.default_room_id(OWNER): "channel",
        str(uuid.uuid4()): "channel",
        str(uuid.uuid4()): "group-dm",
        str(uuid.uuid4()): "dm",
    }
    for room_id, kind in rooms.items():
        members = (
            [_human("user_C", "owner"), agent] if kind == "dm" else [_human("user_A", "owner"), _human("user_C"), agent]
        )
        await _room(room_id, kind, members)
    await repo.ensure_user_stream(OWNER, "user_C")
    for room_id in rooms:
        await messaging.require_room_member(OWNER, "user_C", room_id)

    await messaging.revoke_human_membership(OWNER, "user_C")

    for room_id in rooms:
        assert await repo.get_member(room_id, "human", "user_C") is None, room_id
        with pytest.raises(messaging.ApiProblem):
            await messaging.require_room_member(OWNER, "user_C", room_id)
    assert await repo.list_rooms_for_member(OWNER, "human", "user_C") == []
