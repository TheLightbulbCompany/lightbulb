"""An existing workspace feed room (spec 2026-10-14 §4.2, task 8a): one hidden channel per workspace, identified
by id, its topic the team's brief. Nothing seeds or reads it any more; these pin the rules its rows keep, and the
#general autojoin's removal window."""

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
        "topic": "Only money.",
    }
    member = {"kind": "human", "id": "user_a", "ownerId": OWNER, "role": "member", "joinedSeq": 0}
    summary = messaging.RoomSummary(**messaging._room_summary_fields(meta, member, None))
    assert summary.model_dump(by_alias=True)["feed"] is True
    other = messaging._room_summary_fields({**meta, "roomId": str(uuid.uuid4())}, member, None)
    assert other["feed"] is False


@pytest.fixture
def workspace(table, monkeypatch):  # noqa: F811
    from core.repositories.workspace_repo import WorkspaceRepo

    async def owner_row(*_args, **_kwargs):
        return SimpleNamespace(
            status="active", starter_agent_ids=STARTERS, seeded_agent_ids=None, provisioner_user_id="user_A"
        )

    roster = [{"id": agent_id, "name": key.title()} for key, agent_id in STARTERS.items()]
    monkeypatch.setattr(WorkspaceRepo, "resolve_owner_row", owner_row)
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    monkeypatch.setattr(messaging, "human_display_name", lambda user: asyncio.sleep(0, result=f"Name {user}"))
    monkeypatch.setattr(messaging, "_validate_human_in_owner", AsyncMock())
    monkeypatch.setattr(
        messaging.clerk_admin,
        "list_organization_members",
        AsyncMock(return_value=[{"user_id": "user_A"}, {"user_id": "user_B"}]),
    )
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_A"}))


GENERAL = messaging.default_room_id(OWNER)


def _human(user_id: str, role: str = "member") -> dict:
    return {"kind": "human", "id": user_id, "role": role, "listenMode": None, "displayName": f"Name {user_id}"}


async def _feed() -> dict:
    """An existing workspace feed (nothing seeds one any more): people only, the brief as its topic, and the CEO's
    top-level post a Discuss reply can thread under."""
    members = [_human("user_A", "owner"), _human("user_B")]
    await repo.create_room(
        owner_id=OWNER,
        room_id=FEED,
        client_room_id=f"workspace-feed-v1:{OWNER}",
        creator_user_id="user_A",
        name="feed",
        kind="channel",
        members=members,
        req_hash=repo.request_hash("feed", "channel", members),
        extra={"topic": "Keep us posted on customers and money."},
    )
    item, _ = await repo.create_message(
        room_id=FEED,
        owner_id=OWNER,
        idemp_pk=repo.owner_pk(OWNER),
        idemp_sk=repo.agent_idemp_sk(f"feed-welcome:{OWNER}"),
        req_hash="w",
        author={"kind": "agent", "id": CEO, "displayName": "Ceo"},
        text="**This is our feed.**",
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=CEO,
        client_message_id=None,
        author_member_check=False,  # the CEO writes the feed as an author, never a member
    )
    return item


async def _general() -> None:
    members = [_human("user_A", "owner")]
    await repo.create_room(
        owner_id=OWNER,
        room_id=GENERAL,
        client_room_id="general",
        creator_user_id="user_A",
        name="general",
        kind="channel",
        members=members,
        req_hash=repo.request_hash("general", "channel", members),
    )


@pytest.mark.asyncio
async def test_humans_are_never_added_or_removed_in_the_feed_by_hand(workspace):
    from core.auth import AuthContext

    await _feed()
    owner = AuthContext(user_id=OWNER)
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


# ---- posting ---------------------------------------------------------------


def _post(**over) -> messaging.AgentPostRequest:
    return messaging.AgentPostRequest.model_validate({"eventId": "post:x:1", "agentId": AGENT, "text": "hi", **over})


def test_the_feed_is_no_longer_a_post_target():
    """Nothing seeds or reads the feed any more: an older script's `--feed` post is refused, never sent elsewhere."""
    with pytest.raises(ValidationError):
        _post(feed=True, postReason="why")


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

    await _feed()
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    member, creator = AuthContext(user_id=OWNER), AuthContext(user_id=OWNER)
    detail = await messaging.patch_room(member, FEED, messaging.PatchRoomRequest(topic="Only money."))
    assert detail.topic == "Only money."
    for request in (messaging.PatchRoomRequest(name="x"), messaging.PatchRoomRequest(archived=True)):
        with pytest.raises(messaging.ApiProblem):
            await messaging.patch_room(creator, FEED, request)
    with pytest.raises(messaging.ApiProblem):
        await messaging.leave_room(member, FEED)
    with pytest.raises(messaging.ApiProblem):
        await messaging.delete_room(creator, FEED)


# ---- round 2: the removal window (#general autojoin) ----------------------------


def _active(stream) -> bool:
    return bool(stream and stream.get("active"))


@pytest.mark.asyncio
async def test_a_seat_that_lands_as_the_stream_goes_off_is_undone(workspace, monkeypatch):
    await _general()
    await repo.ensure_user_stream(OWNER, "user_C")
    before = await repo.get_room(GENERAL)
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")  # revoke finishes right here
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    assert await messaging.join_default_room(OWNER, "user_C") is False
    assert await repo.get_member(GENERAL, "human", "user_C") is None
    assert (await repo.get_room(GENERAL))["deliveryKey"] != before["deliveryKey"]


# ---- round 3: a seat inside revoke's own window; the undo's revision race -----


def _cancelled():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "revision"}}, "TransactWriteItems")


@pytest.mark.asyncio
async def test_the_undo_retries_a_revision_race_and_still_unseats(workspace, monkeypatch):
    await _general()
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
    assert await messaging.join_default_room(OWNER, "user_C") is False
    assert await repo.get_member(GENERAL, "human", "user_C") is None


@pytest.mark.asyncio
async def test_an_undo_that_cannot_remove_fails_loudly(workspace, monkeypatch):
    from botocore.exceptions import ClientError

    await _general()
    await repo.ensure_user_stream(OWNER, "user_C")
    real_upsert = repo.upsert_member

    async def removed_meanwhile(**kwargs):
        meta = await real_upsert(**kwargs)
        await repo.deactivate_user_stream(OWNER, "user_C")
        return meta

    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", removed_meanwhile)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", AsyncMock(side_effect=_cancelled()))
    with pytest.raises(ClientError):
        await messaging.join_default_room(OWNER, "user_C")


# ---- round 4: the GSI-backed list can miss a fresh seat -----


# ---- Codex review on #4145: human top-level posts, the member cap, --why with --feed -----


@pytest.mark.asyncio
async def test_a_human_top_level_post_in_the_feed_is_refused_but_a_discuss_reply_is_allowed(workspace):
    from core.auth import AuthContext

    await _feed()
    auth = AuthContext(user_id=OWNER)
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


# ---- owner redesign: agents are the feed's AUTHORS, not its members -----


async def _work_rows(table):  # noqa: F811
    return [row for row in table.scan()["Items"] if str(row["sk"]).startswith("WORK#MESSAGE")]


@pytest.mark.asyncio
async def test_a_roster_agent_outside_the_room_posts_to_the_feed(crowded):
    await _feed()
    hire = BIG_ROSTER["hire3"]
    receipt = await messaging.post_agent_message(
        OWNER, _post(roomId=FEED, postReason="why", agentId=hire, text="**Up**")
    )
    assert str(receipt.room_id) == FEED
    assert await repo.get_member(FEED, "agent", hire) is None  # posted as an author, never seated
    posted = await repo.get_message_by_id(FEED, str(receipt.message_id))
    assert posted["text"] == "**Up**" and posted["postReason"] == "why"


@pytest.mark.asyncio
async def test_another_owners_agent_cannot_post_to_the_feed(crowded):
    await _feed()
    stranger = "30000000-0000-4000-8000-000000000001"
    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.post_agent_message(OWNER, _post(roomId=FEED, postReason="why", agentId=stranger))
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

    await _feed()
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    auth = AuthContext(user_id=OWNER)
    await messaging.create_human_message(auth, FEED, _human_reply(root, [BIG_ROSTER["growth"]]))
    rows = await _work_rows(table)
    assert [row["sk"].rsplit("#", 1)[-1] for row in rows] == [BIG_ROSTER["growth"]]
    assert rows[0]["pk"] == f"ROOM#{FEED}"


@pytest.mark.asyncio
async def test_plain_feed_posts_wake_nobody(crowded, table):  # noqa: F811
    from core.auth import AuthContext

    await _feed()
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    await messaging.create_human_message(AuthContext(user_id=OWNER), FEED, _human_reply(root))
    await messaging.post_agent_message(OWNER, _post(roomId=FEED, postReason="why", agentId=CEO, text="@growth look"))
    assert await _work_rows(table) == []


@pytest.mark.asyncio
async def test_a_non_member_agents_discuss_reply_is_accepted_in_its_thread(crowded):
    await _feed()
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
    await _feed()
    stand_in = await messaging.room_agent(FEED, CEO)
    assert (stand_in["kind"], stand_in["id"], stand_in["listenMode"]) == ("agent", CEO, "mentions")
    assert await messaging.room_agent(FEED, "30000000-0000-4000-8000-000000000001") is None
    assert await messaging.room_agent(messaging.default_room_id(OWNER), CEO) is None


@pytest.mark.asyncio
async def test_the_work_consumer_wakes_a_feed_author_it_never_seated(crowded):
    from core.auth import AuthContext
    from core.services import openclaw_work as work

    await _feed()
    root = uuid.UUID((await repo.list_messages(FEED, 0, 10))[0][0]["messageId"])
    growth = BIG_ROSTER["growth"]
    await messaging.create_human_message(AuthContext(user_id=OWNER), FEED, _human_reply(root, [growth]))
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
    await _feed()
    for reason in (None, "   "):
        with pytest.raises(messaging.ApiProblem) as exc:
            await messaging.create_agent_message(OWNER, _agent_message("e-none", reason=reason))
        assert exc.value.error.code == "INVALID_REQUEST"
    root = (await repo.list_messages(FEED, 0, 10))[0][0]["messageId"]
    await messaging.create_agent_message(OWNER, _agent_message("e-reply", root=root, text="Thanks"))


@pytest.mark.asyncio
async def test_a_changed_reason_under_one_event_id_is_a_conflict_not_a_replay(crowded):
    await _feed()
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

    await _feed()
    auth = AuthContext(user_id=OWNER)  # the feed's owner
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
