"""The workspace feed room (spec 2026-10-14 §4.2, task 8a): one hidden channel per workspace,
identified by id, its topic the team's brief; agents post to it with ``feed: true`` and a reason."""

import asyncio
import threading
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
    # Agents are the feed's authors, not its members: nobody but people sits in the room.
    assert not [row for row in members if row["kind"] == "agent"]
    messages, _ = await repo.list_messages(FEED, 0, 100)
    assert len(messages) == 1
    assert messages[0]["author"]["id"] == CEO
    assert messages[0]["text"] == messaging.FEED_WELCOME
    for place in ("four places", "Chat", "Activity", "Work", "Library", "Discuss", "💡"):
        assert place in messaging.FEED_WELCOME
    assert "five places" not in messaging.FEED_WELCOME and "Ideas for" not in messaging.FEED_WELCOME
    # The general room is untouched.
    assert await repo.get_room(messaging.default_room_id(OWNER)) is None


@pytest.mark.asyncio
async def test_a_super_agent_feed_welcome_promises_only_what_runs(workspace, monkeypatch):
    from core.services import super_agent_flags

    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    await messaging.ensure_feed_room(OWNER, "user_A")
    messages, _ = await repo.list_messages(FEED, 0, 100)
    text = messages[0]["text"]
    assert text == messaging.FEED_WELCOME_SUPER_AGENT
    assert "three" not in text and "💡" not in text and "Work for tasks" not in text
    for place in ("Home", "Needs you", "Ideas", "feed", "Discuss", "brief"):
        assert place in text


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
async def test_two_different_users_racing_get_one_room_one_welcome_no_error(workspace, monkeypatch):
    from core.auth import AuthContext

    # moto restores the whole table on a failed transaction and is not thread-safe: a losing
    # create's restore can wipe the winner's rows, which DynamoDB never does. One call at a time
    # (test_messaging_repo's pattern); the readers still interleave between awaits.
    atomic = threading.Lock()
    real = repo.run_in_thread

    async def run_in_thread(call, *args, **kwargs):
        def locked(*a, **kw):
            with atomic:
                return call(*a, **kw)

        return await real(locked, *args, **kwargs)

    monkeypatch.setattr(repo, "run_in_thread", run_in_thread)
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
        author_member_check=False,  # an agent writes the feed as an author
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


# ---- round 4: the GSI-backed list can miss a fresh seat; a failed undo frees the marker -----


@pytest.mark.asyncio
@pytest.mark.parametrize("room", ["feed", "general"])
async def test_revoke_removes_a_feed_or_general_seat_the_member_index_has_not_caught(workspace, monkeypatch, room):
    """``list_rooms_for_member`` reads an eventually consistent GSI: a seat committed just before
    stream-off may be missing from it. Revoke removes the feed and #general seats regardless."""
    await messaging.ensure_feed_room(OWNER)
    room_id = FEED
    if room == "general":
        room_id = messaging.default_room_id(OWNER)
        owner = {"kind": "human", "id": "user_A", "role": "owner", "listenMode": None, "displayName": "A"}
        await repo.create_room(
            owner_id=OWNER,
            room_id=room_id,
            client_room_id="general",
            creator_user_id="user_A",
            name="general",
            kind="channel",
            members=[owner],
            req_hash=repo.request_hash("general", "channel", [owner]),
        )
    await repo.ensure_user_stream(OWNER, "user_C")
    assert await messaging._autojoin(OWNER, room_id, "user_C") is True
    before = await repo.get_room(room_id)
    monkeypatch.setattr(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=[]))
    await messaging.revoke_human_membership(OWNER, "user_C")
    assert await repo.get_member(room_id, "human", "user_C") is None
    assert (await repo.get_room(room_id))["deliveryKey"] != before["deliveryKey"]


@pytest.mark.asyncio
async def test_a_failed_undo_still_frees_the_feed_marker_for_a_later_rejoin(workspace, monkeypatch):
    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(side_effect=_cancelled()))
    with pytest.raises(Exception):
        await messaging.join_feed_room(OWNER, "user_C")
    # The marker is free: claiming it again succeeds.
    assert await repo.claim_autojoin(FEED, "user_C") is True


@pytest.mark.asyncio
async def test_a_failed_marker_release_never_hides_the_undo_error(workspace, monkeypatch):
    await messaging.ensure_feed_room(OWNER)
    await repo.ensure_user_stream(OWNER, "user_C")
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(side_effect=_cancelled()))
    monkeypatch.setattr(messaging.messaging_repo, "release_autojoin", AsyncMock(side_effect=RuntimeError("release")))
    with pytest.raises(Exception) as exc:
        await messaging.join_feed_room(OWNER, "user_C")
    assert "revision" in str(exc.value)  # the undo's TransactionCanceledException, not "release"


# ---- Codex review on #4145: human top-level posts, the member cap, --why with --feed -----


@pytest.mark.asyncio
async def test_a_human_top_level_post_in_the_feed_is_refused_but_a_discuss_reply_is_allowed(workspace):
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    auth = AuthContext(user_id="user_A", org_id=OWNER)
    welcome = (await repo.list_messages(FEED, 0, 10))[0][0]

    def request(root):
        return messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(), text="hello", reply_to_message_id=None, thread_root_message_id=root
        )

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_human_message(auth, FEED, request(None))
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    reply = await messaging.create_human_message(auth, FEED, request(uuid.UUID(welcome["messageId"])))
    assert reply.text == "hello"


BIG_ROSTER = {**STARTERS, **{f"hire{i}": f"20000000-0000-4000-8000-{i:012d}" for i in range(10)}}  # 16 agents
PEOPLE = [{"user_id": "user_A"}, *({"user_id": f"user_{i:02d}"} for i in range(19))]  # 20 humans


@pytest.fixture
def crowded(workspace, monkeypatch):
    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in BIG_ROSTER.items()]
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    monkeypatch.setattr(messaging.clerk_admin, "list_organization_members", AsyncMock(return_value=PEOPLE))


@pytest.mark.asyncio
async def test_twenty_humans_are_all_seated_and_no_agent_sits_in_the_feed(crowded):
    assert await messaging.ensure_feed_room(OWNER, "user_A") == FEED
    members = await repo.list_members(FEED)
    assert {row["id"] for row in members if row["kind"] == "human"} == {row["user_id"] for row in PEOPLE}
    assert not [row for row in members if row["kind"] == "agent"]


@pytest.mark.asyncio
async def test_a_human_who_cannot_be_seated_fails_the_feed_read_loudly(crowded, monkeypatch):
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    monkeypatch.setattr(
        messaging.messaging_repo, "upsert_member", AsyncMock(side_effect=repo.MemberLimitExceeded("full"))
    )
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.get_feed(AuthContext(user_id="user_late", org_id=OWNER))
    assert exc.value.error.code == "MEMBER_LIMIT"


# ---- owner redesign: agents are the feed's AUTHORS, not its members -----


async def _work_rows(table):  # noqa: F811
    return [row for row in table.scan()["Items"] if str(row["sk"]).startswith("WORK#MESSAGE")]


@pytest.mark.asyncio
async def test_a_roster_agent_outside_the_room_posts_to_the_feed(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    hire = BIG_ROSTER["hire3"]
    receipt = await messaging.post_agent_message(OWNER, _post(feed=True, postReason="why", agentId=hire, text="**Up**"))
    assert str(receipt.room_id) == FEED
    assert await repo.get_member(FEED, "agent", hire) is None  # posted as an author, never seated
    posts = (await messaging.agent_feed(OWNER))["posts"]
    assert posts[-1]["text"] == "**Up**" and posts[-1]["postReason"] == "why"


@pytest.mark.asyncio
async def test_another_owners_agent_cannot_post_to_the_feed(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    stranger = "30000000-0000-4000-8000-000000000001"
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(feed=True, postReason="why", agentId=stranger))
    assert exc.value.error.code == "MEMBER_NOT_FOUND"


def _human_reply(root, mentions=()):
    return messaging.CreateMessageRequest(
        client_message_id=uuid.uuid4(),
        text="what about churn?",
        mentions=[messaging.MemberRef(kind="agent", id=agent_id) for agent_id in mentions],
        reply_to_message_id=None,
        thread_root_message_id=root,
    )


@pytest.mark.asyncio
async def test_a_discuss_reply_that_mentions_an_agent_wakes_exactly_that_agent(crowded, table):  # noqa: F811
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    auth = AuthContext(user_id="user_A", org_id=OWNER)
    await messaging.create_human_message(auth, FEED, _human_reply(root, [BIG_ROSTER["growth"]]))
    rows = await _work_rows(table)
    assert [row["sk"].rsplit("#", 1)[-1] for row in rows] == [BIG_ROSTER["growth"]]
    assert rows[0]["pk"] == f"ROOM#{FEED}"


@pytest.mark.asyncio
async def test_plain_feed_posts_wake_nobody(crowded, table):  # noqa: F811
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    await messaging.create_human_message(AuthContext(user_id="user_A", org_id=OWNER), FEED, _human_reply(root))
    await messaging.post_agent_message(OWNER, _post(feed=True, postReason="why", agentId=CEO, text="@growth look"))
    assert await _work_rows(table) == []


@pytest.mark.asyncio
async def test_a_non_member_agents_discuss_reply_is_accepted_in_its_thread(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    root = (await repo.list_messages(FEED, 0, 10))[0][0]["messageId"]
    agent = BIG_ROSTER["growth"]
    receipt = await messaging.create_agent_message(
        OWNER,
        messaging.AgentMessageRequest.model_validate(
            {
                "eventId": "reply-1",
                "roomId": FEED,
                "agentId": agent,
                "text": "Churn is flat.",
                "replyToMessageId": None,
                "threadRootMessageId": root,
            }
        ),
    )
    stored = await repo.get_message_by_id(FEED, str(receipt.message_id))
    assert stored["threadRootMessageId"] == root and stored["author"]["id"] == agent
    # Another room still requires membership, exactly as before.
    other = messaging.default_room_id(OWNER)
    with pytest.raises(messaging.ApiProblem):
        await messaging.create_agent_message(
            OWNER,
            messaging.AgentMessageRequest.model_validate(
                {
                    "eventId": "x",
                    "roomId": other,
                    "agentId": agent,
                    "text": "hi",
                    "replyToMessageId": None,
                    "threadRootMessageId": None,
                }
            ),
        )


def test_the_feed_rule_wakes_only_mentioned_roster_agents_in_a_thread():
    roster = {CEO, STARTERS["growth"]}
    mention = [{"kind": "agent", "id": STARTERS["growth"]}, {"kind": "agent", "id": "not-on-roster"}]
    pick = messaging.select_dispatch_targets
    assert pick([], mention, None, room_kind="channel", feed_roster=roster, in_thread=True) == [
        {"agentId": STARTERS["growth"], "eventKind": "user_request"}
    ]
    assert pick([], mention, None, room_kind="channel", feed_roster=roster, in_thread=False) == []
    assert pick([], [], None, room_kind="channel", feed_roster=roster, in_thread=True) == []
    assert pick([], mention, STARTERS["growth"], room_kind="channel", feed_roster=roster, in_thread=True) == []


@pytest.mark.asyncio
async def test_room_agent_stands_in_a_roster_agent_for_the_feed_only(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    stand_in = await messaging.room_agent(FEED, CEO)
    assert (stand_in["kind"], stand_in["id"], stand_in["listenMode"]) == ("agent", CEO, "mentions")
    assert await messaging.room_agent(FEED, "30000000-0000-4000-8000-000000000001") is None
    assert await messaging.room_agent(messaging.default_room_id(OWNER), CEO) is None


def test_a_feed_post_requires_a_reason():
    with pytest.raises(ValidationError):
        _post(feed=True)
    with pytest.raises(ValidationError):
        _post(feed=True, postReason="   ")  # blank is no reason
    assert _post(feed=True, postReason="why").post_reason == "why"
    assert _post().post_reason is None  # other targets keep it optional


@pytest.mark.asyncio
async def test_the_work_consumer_wakes_a_feed_author_it_never_seated(crowded):
    from core.auth import AuthContext
    from core.services import openclaw_work as work

    await messaging.ensure_feed_room(OWNER, "user_A")
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    growth = BIG_ROSTER["growth"]
    await messaging.create_human_message(
        AuthContext(user_id="user_A", org_id=OWNER), FEED, _human_reply(root, [growth])
    )
    seq = int((await repo.get_room(FEED))["lastSeq"])
    worker = work.OpenClawWorkWorker(sqs_client=None)
    dispatched = AsyncMock(return_value=True)
    worker._dispatch_message = dispatched
    await worker._room_dispatch({"roomId": FEED, "agentId": growth, "throughSeq": seq})
    dispatched.assert_awaited_once()
    member, message = dispatched.await_args.args[1], dispatched.await_args.args[2]
    assert member["id"] == growth and message["messageSeq"] == seq
    assert int((await repo.get_agent_delivery(FEED, growth))["lastSeq"]) == seq


# ---- Codex re-review at 1539bbf72 -----


def _agent_message(event_id, *, root=None, reason=None, text="**Paid**"):
    return messaging.AgentMessageRequest.model_validate(
        {
            "eventId": event_id,
            "roomId": FEED,
            "agentId": CEO,
            "text": text,
            "replyToMessageId": None,
            "threadRootMessageId": root,
            **({"postReason": reason} if reason is not None else {}),
        }
    )


@pytest.mark.asyncio
async def test_a_direct_top_level_feed_post_needs_a_reason_but_a_discuss_reply_does_not(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    for reason in (None, "   "):
        with pytest.raises(messaging.ApiProblem) as exc:
            await messaging.create_agent_message(OWNER, _agent_message("e-none", reason=reason))
        assert exc.value.error.code == "INVALID_REQUEST"
    root = (await repo.list_messages(FEED, 0, 10))[0][0]["messageId"]
    await messaging.create_agent_message(OWNER, _agent_message("e-reply", root=root, text="Thanks"))


@pytest.mark.asyncio
async def test_a_changed_reason_under_one_event_id_is_a_conflict_not_a_replay(crowded):
    await messaging.ensure_feed_room(OWNER, "user_A")
    first = await messaging.create_agent_message(OWNER, _agent_message("e-1", reason="Money in."))
    assert (await messaging.create_agent_message(OWNER, _agent_message("e-1", reason="Money in."))).message_id == (
        first.message_id
    )
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_agent_message(OWNER, _agent_message("e-1", reason="A customer replied."))
    assert exc.value.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "human"])
async def test_nobody_changes_feed_membership_by_hand(crowded, kind):
    from core.auth import AuthContext

    await messaging.ensure_feed_room(OWNER, "user_A")
    auth = AuthContext(user_id="user_A", org_id=OWNER)  # the feed's owner
    member_id = CEO if kind == "agent" else "user_05"
    listen = "active" if kind == "agent" else None
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(
            auth,
            FEED,
            messaging.UpsertRoomMemberRequest(kind=kind, id=member_id, role="member", listen_mode=listen),
        )
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.remove_member(auth, FEED, kind, member_id)
    assert exc.value.error.code == "ROOM_FORBIDDEN"
    assert await repo.get_member(FEED, "agent", CEO) is None


@pytest.mark.asyncio
async def test_a_teammate_who_joins_while_the_feed_is_being_made_is_seated(crowded, monkeypatch):
    """C joins Clerk after the creator's snapshot but before the room exists: C's own webhook
    join no-ops (no room yet), so the creation seats everyone Clerk lists once the room exists."""
    late = {"user_id": "user_late"}
    reads = {"n": 0}

    async def members(*_args, **_kwargs):
        reads["n"] += 1
        if reads["n"] == 1:
            assert await messaging.join_feed_room(OWNER, "user_late") is False  # the webhook, too early
            return PEOPLE
        return [*PEOPLE, late]

    monkeypatch.setattr(messaging.clerk_admin, "list_organization_members", members)
    await messaging.ensure_feed_room(OWNER, "user_A")
    assert await repo.get_member(FEED, "human", "user_late") is not None
