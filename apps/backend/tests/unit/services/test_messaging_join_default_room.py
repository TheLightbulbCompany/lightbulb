"""A teammate who joins the workspace lands in the starter #general room — once."""

from unittest.mock import AsyncMock

import pytest
from core.services import messaging
from core.services.messaging import ApiProblem

OWNER, USER = "org_o", "user_m"
ROOM = messaging.default_room_id(OWNER)
META = {"roomId": ROOM, "ownerId": OWNER, "kind": "channel", "revision": 2, "lastSeq": 3}


@pytest.fixture
def repo(monkeypatch):
    fakes = {
        "get_room": AsyncMock(return_value=META),
        "get_member": AsyncMock(return_value=None),
        "claim_autojoin": AsyncMock(return_value=True),
        "release_autojoin": AsyncMock(),
        "upsert_member": AsyncMock(return_value={**META, "revision": 3}),
        "ensure_user_stream": AsyncMock(return_value="key"),
        # The post-seat re-check (a removal that landed meanwhile undoes the seat).
        "get_user_stream": AsyncMock(return_value={"active": True, "channelKey": "key"}),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(messaging.messaging_repo, name, fake)
    monkeypatch.setattr(messaging, "_validate_human_in_owner", AsyncMock())
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Mia"))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_emit_member_added", lambda *a, **k: fakes.setdefault("emitted", (a, k)))
    return fakes


@pytest.mark.asyncio
async def test_adds_a_non_member_once_as_a_plain_human_member(repo):
    assert await messaging.join_default_room(OWNER, USER) is True
    repo["claim_autojoin"].assert_awaited_once_with(ROOM, USER)
    kwargs = repo["upsert_member"].await_args.kwargs
    assert (kwargs["room_id"], kwargs["owner_id"]) == (ROOM, OWNER)
    assert kwargs["member"] == {
        "kind": "human",
        "id": USER,
        "displayName": "Mia",
        "mentionHandle": None,
        "role": "member",
        "listenMode": None,
    }
    repo["ensure_user_stream"].assert_awaited_once_with(OWNER, USER)
    assert repo["emitted"] == ((OWNER, USER, "human"), {"by": "system"})
    messaging._publish_room_metadata.assert_awaited_once_with({**META, "revision": 3})


@pytest.mark.asyncio
async def test_an_existing_member_is_a_no_op(repo):
    repo["get_member"].return_value = {"kind": "human", "id": USER}
    assert await messaging.join_default_room(OWNER, USER) is False
    repo["claim_autojoin"].assert_not_awaited()
    repo["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_removed_member_is_never_re_added(repo):
    # Marker present (claim fails), member row gone: they were removed after the first join.
    repo["claim_autojoin"].return_value = False
    assert await messaging.join_default_room(OWNER, USER) is False
    repo["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_room_is_a_no_op(repo):
    repo["get_room"].return_value = None
    assert await messaging.join_default_room(OWNER, USER) is False
    repo["claim_autojoin"].assert_not_awaited()
    repo["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_user_outside_the_org_is_rejected_before_the_marker(repo, monkeypatch):
    monkeypatch.setattr(
        messaging,
        "_validate_human_in_owner",
        AsyncMock(side_effect=ApiProblem("MEMBER_NOT_FOUND", "That human is not in this workspace.")),
    )
    with pytest.raises(ApiProblem):
        await messaging.join_default_room(OWNER, USER)
    repo["claim_autojoin"].assert_not_awaited()
    repo["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_add_releases_the_marker_so_a_later_call_retries(repo):
    repo["upsert_member"].side_effect = RuntimeError("ddb down")
    with pytest.raises(RuntimeError):
        await messaging.join_default_room(OWNER, USER)
    repo["release_autojoin"].assert_awaited_once_with(ROOM, USER)


@pytest.mark.asyncio
async def test_a_full_room_keeps_the_marker(repo):
    repo["upsert_member"].side_effect = messaging.messaging_repo.MemberLimitExceeded("full")
    assert await messaging.join_default_room(OWNER, USER) is False
    repo["release_autojoin"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("room_id,marked", [(ROOM, True), ("00000000-0000-4000-8000-000000000001", False)])
async def test_removing_a_human_from_general_marks_them_so_polls_never_reseat(monkeypatch, repo, room_id, marked):
    monkeypatch.setattr(
        messaging.messaging_repo, "list_members", AsyncMock(return_value=[{"kind": "human", "id": USER}])
    )
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(return_value=META))

    await messaging._remove_member_with_notices({**META, "roomId": room_id}, "human", USER)

    if marked:
        repo["claim_autojoin"].assert_awaited_once_with(ROOM, USER)
    else:
        repo["claim_autojoin"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_removal_marker_never_fails_the_committed_removal(monkeypatch, repo):
    monkeypatch.setattr(
        messaging.messaging_repo, "list_members", AsyncMock(return_value=[{"kind": "human", "id": USER}])
    )
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(return_value=META))
    repo["claim_autojoin"].side_effect = RuntimeError("ddb down")

    assert await messaging._remove_member_with_notices(META, "human", USER) == META
