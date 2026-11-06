"""Adding someone to a DM starts a new group chat; the DM stays private and untouched (founder ruling, 7 Nov 2026)."""

from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import messaging_repo as repo
from core.services import messaging
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, USER_A, USER_B, _member, _post, _room, table  # noqa: F401


@pytest.fixture
def wired(monkeypatch, table):  # noqa: F811
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        messaging,
        "_resolved_member",
        AsyncMock(
            side_effect=lambda _o, m, _r: _member(m.kind, m.id, handle="lightbulb" if m.kind == "agent" else None)
        ),
    )
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="stream"))
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(side_effect=lambda meta, *_a, **_k: meta))


async def test_the_new_member_cannot_read_the_old_dm_and_the_dm_is_unchanged(wired):
    dm_id, _ = await _room(
        kind="dm", name=None, members=[_member("human", USER_A, "owner"), _member("agent", FRIDAY, handle="lightbulb")]
    )
    await _post(dm_id, text="my salary is 90k", client_id="private-1")
    await repo.put_noted(dm_id, "00000000-0000-4000-8000-000000000001", "Salary is 90k.")
    before = await repo.get_room(dm_id)
    ann = AuthContext(user_id=USER_A, org_id=OWNER)

    group = await messaging.upsert_member(
        ann, dm_id, messaging.UpsertRoomMemberRequest(kind="human", id=USER_B, role="member", listen_mode=None)
    )

    assert group["roomId"] != dm_id and group["kind"] == "group-dm"
    assert {(m["kind"], m["id"]) for m in await repo.list_members(group["roomId"])} == {
        ("human", USER_A),
        ("human", USER_B),
        ("agent", FRIDAY),
    }
    assert group.get("topic") is None and await repo.list_noted(group["roomId"]) == []
    # The DM: same kind, members, revision and notes; Bob is no member of it, so every door is shut to him.
    after = await repo.get_room(dm_id)
    assert after["kind"] == "dm" and after["revision"] == before["revision"]
    assert {(m["kind"], m["id"]) for m in await repo.list_members(dm_id)} == {("human", USER_A), ("agent", FRIDAY)}
    assert len(await repo.list_noted(dm_id)) == 1
    bob = AuthContext(user_id=USER_B, org_id=OWNER)
    with pytest.raises(messaging.ApiProblem):
        await messaging.require_room_member(OWNER, USER_B, dm_id)
    with pytest.raises(messaging.ApiProblem):
        await messaging.list_room_memory(bob, dm_id)

    # A retried add opens the same group chat, not a second one.
    again = await messaging.upsert_member(
        ann, dm_id, messaging.UpsertRoomMemberRequest(kind="human", id=USER_B, role="member", listen_mode=None)
    )
    assert again["roomId"] == group["roomId"]
