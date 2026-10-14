"""The workspace feed room (spec 2026-10-14 §4.2, task 8a): one hidden channel per workspace,
identified by id, its topic the team's brief; agents post to it with ``feed: true`` and a reason."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.repositories import messaging_repo as repo
from core.services import messaging
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401
from tests.unit.services.test_agent_message_attachments import AGENT, OWNER, created  # noqa: F401

STARTERS = {
    "ceo": "10000000-0000-4000-8000-000000000001",
    "engineering": "10000000-0000-4000-8000-000000000002",
    "finance": "10000000-0000-4000-8000-000000000003",
    "growth": "10000000-0000-4000-8000-000000000004",
    "analytics": "10000000-0000-4000-8000-000000000005",
    "customer_operations": "10000000-0000-4000-8000-000000000006",
}
CEO = STARTERS["ceo"]
FEED = repo.feed_room_id(OWNER)


def test_feed_room_id_is_deterministic_per_owner_and_not_general():
    assert repo.feed_room_id(OWNER) == FEED == str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-feed-room|{OWNER}"))
    assert repo.feed_room_id("org_other") != FEED
    assert FEED != messaging.default_room_id(OWNER)


def test_the_feed_is_recognised_by_id_never_by_name():
    assert messaging.is_feed_room({"roomId": FEED, "ownerId": OWNER})
    assert not messaging.is_feed_room({"roomId": str(uuid.uuid4()), "ownerId": OWNER, "name": "feed"})
    assert not messaging.is_feed_room({"roomId": repo.feed_room_id("org_other"), "ownerId": OWNER})


def test_the_summary_flags_the_feed_so_clients_hide_it():
    meta = {
        "roomId": FEED,
        "ownerId": OWNER,
        "kind": "channel",
        "revision": 1,
        "lastSeq": 1,
        "updatedAt": "2026-10-14T00:00:00Z",
        "createdAt": "2026-10-14T00:00:00Z",
        "topic": messaging.FEED_DEFAULT_TOPIC,
    }
    member = {"kind": "human", "id": "user_a", "ownerId": OWNER, "role": "member", "joinedSeq": 0}
    summary = messaging.RoomSummary(**messaging._room_summary_fields(meta, member, None))
    assert summary.model_dump(by_alias=True)["feed"] is True
    other = messaging._room_summary_fields({**meta, "roomId": str(uuid.uuid4())}, member, None)
    assert other["feed"] is False


@pytest.fixture
def workspace(table, monkeypatch):  # noqa: F811
    from core.repositories.paperclip_repo import PaperclipRepo

    async def owner_row(*_args, **_kwargs):
        return SimpleNamespace(
            status="active", starter_agent_ids=STARTERS, seeded_agent_ids=None, provisioner_user_id="user_A"
        )

    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in STARTERS.items()]
    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", owner_row)
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    monkeypatch.setattr(messaging, "human_display_name", lambda user: asyncio.sleep(0, result=f"Name {user}"))
    monkeypatch.setattr(messaging, "_validate_human_in_owner", AsyncMock())
    monkeypatch.setattr(
        messaging.clerk_admin,
        "list_organization_members",
        AsyncMock(return_value=[{"user_id": "user_A"}, {"user_id": "user_B"}]),
    )
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_A"}))


@pytest.mark.asyncio
async def test_ensure_feed_room_twice_makes_one_room_with_one_welcome(workspace):
    assert await messaging.ensure_feed_room(OWNER, "user_A") == FEED
    assert await messaging.ensure_feed_room(OWNER, "user_A") == FEED

    meta = await repo.get_room(FEED)
    assert (meta["name"], meta["kind"], meta["topic"]) == ("feed", "channel", messaging.FEED_DEFAULT_TOPIC)
    assert messaging.FEED_DEFAULT_TOPIC == (
        "Keep us posted on customers, money and what the team finished. Skip engineering details."
    )
    members = await repo.list_members(FEED)
    # Every workspace human (the creator and the teammate seated through the AUTOJOIN marker).
    assert {row["id"] for row in members if row["kind"] == "human"} == {"user_A", "user_B"}
    # Every roster agent, on mentions: a plain post wakes nobody, an @mention does.
    assert {row["id"]: row["listenMode"] for row in members if row["kind"] == "agent"} == {
        agent_id: "mentions" for agent_id in STARTERS.values()
    }
    messages, _ = await repo.list_messages(FEED, 0, 100)
    assert len(messages) == 1
    assert messages[0]["author"]["id"] == CEO
    assert messages[0]["text"] == messaging.FEED_WELCOME
    for place in ("Chat", "Activity", "Work", "Ideas", "Library", "Discuss"):
        assert place in messaging.FEED_WELCOME
    # The general room is untouched.
    assert await repo.get_room(messaging.default_room_id(OWNER)) is None


@pytest.mark.asyncio
async def test_a_later_teammate_joins_once_and_workspace_membership_alone_decides(workspace):
    await messaging.ensure_feed_room(OWNER, "user_A")
    assert await messaging.join_feed_room(OWNER, "user_C") is True
    assert await messaging.join_feed_room(OWNER, "user_C") is False
    before = await repo.get_room(FEED)
    # A workspace removal still removes and rotates the key...
    await messaging.revoke_human_membership(OWNER, "user_C")
    assert await repo.get_member(FEED, "human", "user_C") is None
    assert (await repo.get_room(FEED))["deliveryKey"] != before["deliveryKey"]
    assert await messaging.join_feed_room(OWNER, "user_C") is False  # stream off: no seat
    # ...and a real rejoin to the workspace re-seats them.
    await repo.ensure_user_stream(OWNER, "user_C", rejoin=True)
    assert await messaging.join_feed_room(OWNER, "user_C") is True


@pytest.mark.asyncio
async def test_the_first_reader_is_a_member_and_the_workspace_creator_owns_the_feed(workspace):
    from core.auth import AuthContext

    await messaging.get_feed(AuthContext(user_id="user_B", org_id=OWNER))
    roles = {row["id"]: row["role"] for row in await repo.list_members(FEED) if row["kind"] == "human"}
    assert roles == {"user_A": "owner", "user_B": "member"}


@pytest.mark.asyncio
async def test_two_different_users_racing_get_one_room_one_welcome_no_error(workspace):
    from core.auth import AuthContext

    results = await asyncio.gather(
        messaging.get_feed(AuthContext(user_id="user_B", org_id=OWNER)),
        messaging.get_feed(AuthContext(user_id="user_A", org_id=OWNER)),
        messaging.ensure_feed_room(OWNER, "user_A"),
    )
    assert {str(results[0].room_id), str(results[1].room_id), results[2]} == {FEED}
    messages, _ = await repo.list_messages(FEED, 0, 100)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_a_stale_create_that_loses_to_another_roster_converges(workspace, monkeypatch):
    """The loser of a create race whose request differs (here: an agent hired in between)
    re-reads the room instead of answering 500."""
    await messaging.ensure_feed_room(OWNER)
    real_get_room = repo.get_room
    calls = {"n": 0}

    async def stale_once(room_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_get_room(room_id)

    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in STARTERS.items()]
    roster.append({"id": "10000000-0000-4000-8000-000000000099", "name": "Hire"})
    monkeypatch.setattr(messaging.messaging_repo, "get_room", stale_once)
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    assert await messaging.ensure_feed_room(OWNER) == FEED
    messages, _ = await repo.list_messages(FEED, 0, 100)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_humans_are_never_added_or_removed_in_the_feed_by_hand(workspace):
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    owner = AuthContext(user_id="user_A", org_id=OWNER)
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.remove_member(owner, FEED, "human", "user_B")
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert await repo.get_member(FEED, "human", "user_B") is not None
    with pytest.raises(messaging.ApiProblem):
        await messaging.upsert_member(
            owner,
            FEED,
            messaging.UpsertRoomMemberRequest.model_validate(
                {"kind": "human", "id": "user_C", "role": "member", "listenMode": None}
            ),
        )


@pytest.mark.asyncio
async def test_ensure_waits_for_an_active_company(table, monkeypatch):  # noqa: F811
    from core.repositories.paperclip_repo import PaperclipRepo

    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", lambda *_a, **_k: asyncio.sleep(0, result=None))
    assert await messaging.ensure_feed_room(OWNER, "user_A") is None
    assert await repo.get_room(FEED) is None


# ---- posting ---------------------------------------------------------------


def _post(**over) -> messaging.AgentPostRequest:
    return messaging.AgentPostRequest.model_validate({"eventId": "post:x:1", "agentId": AGENT, "text": "hi", **over})


@pytest.mark.asyncio
async def test_feed_true_targets_the_feed_room_with_its_reason(created, monkeypatch):  # noqa: F811
    ensure = AsyncMock(return_value=FEED)
    monkeypatch.setattr(messaging, "ensure_feed_room", ensure)
    await messaging.post_agent_message(OWNER, _post(feed=True, postReason="Revenue is the brief's first ask."))
    ensure.assert_awaited_once_with(OWNER)
    assert created.call_args.kwargs["room_id"] == FEED
    assert created.call_args.kwargs["post_reason"] == "Revenue is the brief's first ask."


@pytest.mark.asyncio
async def test_feed_seats_an_agent_hired_after_the_room_was_made(created, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging, "ensure_feed_room", AsyncMock(return_value=FEED))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(side_effect=[None, {"id": AGENT}]))
    monkeypatch.setattr(messaging.messaging_repo, "claim_autojoin", AsyncMock(return_value=True))
    upsert = AsyncMock(return_value={"roomId": FEED, "revision": 2})
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    await messaging.post_agent_message(OWNER, _post(feed=True, postReason="why"))
    assert upsert.call_args.kwargs["member"]["listenMode"] == "mentions"
    assert created.call_args.kwargs["room_id"] == FEED


def test_feed_is_a_target_like_the_others():
    with pytest.raises(ValidationError):
        _post(feed=True, roomId=str(uuid.uuid4()))


def test_post_reason_is_capped_at_280_characters():
    assert _post(postReason="x" * 280).post_reason == "x" * 280
    with pytest.raises(ValidationError):
        _post(postReason="x" * 281)
    with pytest.raises(ValidationError):
        messaging.AgentMessageRequest.model_validate(
            {
                "eventId": "e",
                "roomId": FEED,
                "agentId": AGENT,
                "text": "hi",
                "replyToMessageId": None,
                "threadRootMessageId": None,
                "postReason": "x" * 281,
            }
        )


@pytest.mark.asyncio
async def test_agent_message_stores_its_post_reason(created):  # noqa: F811
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "e",
            "roomId": str(uuid.UUID(int=9)),
            "agentId": AGENT,
            "text": "hi",
            "replyToMessageId": None,
            "threadRootMessageId": None,
            "postReason": "  A customer replied.  ",
        }
    )
    await messaging.create_agent_message(OWNER, request)
    assert created.call_args.kwargs["post_reason"] == "A customer replied."


@pytest.mark.asyncio
async def test_post_reason_round_trips_through_the_store(table):  # noqa: F811
    from tests.unit.repositories.test_messaging_repo import _member

    room_id = str(uuid.uuid4())
    await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id="c-feed",
        creator_user_id="user_A",
        name="feed",
        kind="channel",
        members=[_member("human", "user_A", role="owner"), _member("agent", AGENT)],
        req_hash="h",
    )
    item, _ = await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.owner_pk(OWNER),
        idemp_sk=repo.agent_idemp_sk("evt-1"),
        req_hash="m",
        author={"kind": "agent", "id": AGENT, "displayName": "CEO"},
        text="**Stripe paid out $1,200**",
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=AGENT,
        client_message_id=None,
        post_reason="Money in is first in your brief.",
    )
    wire = messaging.canonical_message(item).model_dump(by_alias=True, mode="json")
    assert wire["postReason"] == "Money in is first in your brief."
    stored, _ = await repo.list_messages(room_id, 0, 10)
    assert messaging.canonical_message(stored[0]).post_reason == "Money in is first in your brief."
    plain = {**item, "postReason": None}
    plain.pop("postReason")
    assert "postReason" not in messaging.canonical_message(plain).model_dump(by_alias=True, mode="json")


# ---- room guards -----------------------------------------------------------


@pytest.mark.asyncio
async def test_any_member_edits_the_brief_but_nobody_renames_archives_or_leaves(workspace, monkeypatch):
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    member, creator = AuthContext(user_id="user_B", org_id=OWNER), AuthContext(user_id="user_A", org_id=OWNER)
    detail = await messaging.patch_room(member, FEED, messaging.PatchRoomRequest(topic="Only money."))
    assert detail.topic == "Only money."
    for request in (messaging.PatchRoomRequest(name="x"), messaging.PatchRoomRequest(archived=True)):
        with pytest.raises(messaging.ApiProblem):
            await messaging.patch_room(creator, FEED, request)
    with pytest.raises(messaging.ApiProblem):
        await messaging.leave_room(member, FEED)
    with pytest.raises(messaging.ApiProblem):
        await messaging.delete_room(creator, FEED)


@pytest.mark.asyncio
async def test_an_agent_reads_the_brief_and_recent_posts_outside_a_room_turn(workspace):
    """A routine has no signed-in person, so lightbulb_read_rooms reads nothing: the feed's own
    hook-token read is how the writer sees its recent posts and never repeats one."""
    feed = await messaging.agent_feed(OWNER)
    assert feed["roomId"] == FEED and feed["topic"] == messaging.FEED_DEFAULT_TOPIC
    assert [(p["author"], p["text"], p["postReason"]) for p in feed["posts"]] == [("Ceo", messaging.FEED_WELCOME, None)]


@pytest.mark.asyncio
async def test_the_agent_read_returns_top_level_posts_only(workspace):
    await messaging.ensure_feed_room(OWNER)
    (welcome,), _ = await repo.list_messages(FEED, 0, 100)
    await repo.create_message(
        room_id=FEED,
        owner_id=OWNER,
        idemp_pk=repo.owner_pk(OWNER),
        idemp_sk=repo.agent_idemp_sk("discuss-1"),
        req_hash="d",
        author={"kind": "agent", "id": CEO, "displayName": "Ceo"},
        text="A Discuss reply",
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=welcome["messageId"],
        run_id=None,
        dispatch_targets=[],
        source_agent_id=CEO,
        client_message_id=None,
        thread_root_seq=welcome["messageSeq"],
    )
    feed = await messaging.agent_feed(OWNER)
    assert [p["text"] for p in feed["posts"]] == [messaging.FEED_WELCOME]


# ---- round 2: removal window, feed owner choice ----------------------------


async def _seated_c():
    await messaging.ensure_feed_room(OWNER)
    assert await messaging.join_feed_room(OWNER, "user_C") is True
    return await repo.get_room(FEED)


def _active(stream) -> bool:
    return bool(stream and stream.get("active"))


@pytest.mark.asyncio
async def test_a_feed_read_inside_the_removal_window_does_not_reseat(workspace, monkeypatch):
    """Keys rotate before the stream goes off; a read landing in between must not re-seat."""
    await _seated_c()
    real_deactivate = repo.deactivate_user_stream

    async def read_first(owner_id, user_id):
        await messaging.join_feed_room(OWNER, "user_C")  # the removed user's still-valid JWT reads Activity
        return await real_deactivate(owner_id, user_id)

    monkeypatch.setattr(messaging.messaging_repo, "deactivate_user_stream", read_first)
    await messaging.revoke_human_membership(OWNER, "user_C")
    assert await repo.get_member(FEED, "human", "user_C") is None
    assert not _active(await repo.get_user_stream(OWNER, "user_C"))
    # A real rejoin (stream back on) re-seats them: the marker is free again.
    await repo.ensure_user_stream(OWNER, "user_C", rejoin=True)
    assert await messaging.join_feed_room(OWNER, "user_C") is True


@pytest.mark.asyncio
async def test_a_seat_that_lands_as_the_stream_goes_off_is_undone(workspace, monkeypatch):
    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    before = await repo.get_room(FEED)
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")  # revoke finishes right here
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    assert await messaging.join_feed_room(OWNER, "user_C") is False
    assert await repo.get_member(FEED, "human", "user_C") is None
    assert (await repo.get_room(FEED))["deliveryKey"] != before["deliveryKey"]


@pytest.mark.asyncio
async def test_a_departed_creator_never_owns_the_feed(workspace, monkeypatch):
    # The provisioner (user_A) and Clerk's created_by (user_X) have both left; user_B is an admin.
    monkeypatch.setattr(
        messaging.clerk_admin,
        "list_organization_members",
        AsyncMock(return_value=[{"user_id": "user_C"}, {"user_id": "user_B", "role": "org:admin"}]),
    )
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_X"}))
    assert await messaging.ensure_feed_room(OWNER) == FEED
    roles = {row["id"]: row["role"] for row in await repo.list_members(FEED) if row["kind"] == "human"}
    assert roles == {"user_B": "owner", "user_C": "member"}


@pytest.mark.asyncio
async def test_an_inactive_stream_is_not_an_owner_and_the_reader_is_the_last_resort(workspace, monkeypatch):
    from core.auth import AuthContext

    await repo.ensure_user_stream(OWNER, "user_A")
    await repo.deactivate_user_stream(OWNER, "user_A")  # removed, Clerk not caught up yet
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value={}))
    await messaging.get_feed(AuthContext(user_id="user_B", org_id=OWNER))
    roles = {row["id"]: row["role"] for row in await repo.list_members(FEED) if row["kind"] == "human"}
    assert roles == {"user_B": "owner"}


@pytest.mark.asyncio
async def test_a_workspace_with_no_active_human_has_no_feed_404_not_503(workspace, monkeypatch):
    from core.auth import AuthContext

    monkeypatch.setattr(messaging.clerk_admin, "list_organization_members", AsyncMock(return_value=[]))
    for call in (
        messaging.get_feed(AuthContext(user_id="user_Z", org_id=OWNER)),
        messaging.agent_feed(OWNER),
    ):
        with pytest.raises(messaging.ApiProblem) as exc:
            await call
        assert exc.value.error.code == "ROOM_NOT_FOUND"
    assert await repo.get_room(FEED) is None


# ---- round 3: a seat inside revoke's own window; the undo's revision race -----


def _cancelled():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "revision"}}, "TransactWriteItems")


@pytest.mark.asyncio
async def test_a_first_seat_between_revokes_room_list_and_stream_off_is_removed(workspace, monkeypatch):
    """No marker is held for a user not yet seated: a seat landing after revoke listed the rooms
    passes its own re-check (stream still on). Revoke's second pass removes it."""
    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    before = await repo.get_room(FEED)
    real_list = repo.list_rooms_for_member
    calls = {"n": 0}

    async def seat_after_first_list(owner_id, kind, member_id):
        rows = await real_list(owner_id, kind, member_id)
        calls["n"] += 1
        if calls["n"] == 1:
            assert await messaging.join_feed_room(OWNER, "user_C") is True  # lands in the window
        return rows

    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", seat_after_first_list)
    await messaging.revoke_human_membership(OWNER, "user_C")
    assert await repo.get_member(FEED, "human", "user_C") is None
    assert (await repo.get_room(FEED))["deliveryKey"] != before["deliveryKey"]


@pytest.mark.asyncio
async def test_the_undo_retries_a_revision_race_and_still_unseats(workspace, monkeypatch):
    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    real_upsert, real_remove = repo.upsert_member, repo.remove_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    calls = iter([_cancelled()])

    async def flaky(**kwargs):
        exc = next(calls, None)
        if exc is not None:
            raise exc
        return await real_remove(**kwargs)

    monkeypatch.setattr(messaging.messaging_repo, "remove_member", flaky)
    assert await messaging.join_feed_room(OWNER, "user_C") is False
    assert await repo.get_member(FEED, "human", "user_C") is None


@pytest.mark.asyncio
async def test_an_undo_that_cannot_remove_fails_loudly(workspace, monkeypatch):
    from botocore.exceptions import ClientError

    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(side_effect=_cancelled()))
    with pytest.raises(ClientError):
        await messaging.join_feed_room(OWNER, "user_C")
