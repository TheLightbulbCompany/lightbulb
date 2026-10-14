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
        return SimpleNamespace(status="active", starter_agent_ids=STARTERS, seeded_agent_ids=None)

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
async def test_a_later_teammate_joins_the_feed_once_and_a_removal_sticks(workspace):
    await messaging.ensure_feed_room(OWNER, "user_A")
    assert await messaging.join_feed_room(OWNER, "user_C") is True
    assert await messaging.join_feed_room(OWNER, "user_C") is False
    meta = await repo.get_room(FEED)
    await messaging._remove_member_with_notices(meta, "human", "user_C")
    assert await messaging.join_feed_room(OWNER, "user_C") is False
    assert await repo.get_member(FEED, "human", "user_C") is None


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
