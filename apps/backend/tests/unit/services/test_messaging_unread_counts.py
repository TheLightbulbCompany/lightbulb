"""Exact room-list counts (rooms F6, PR 2).

unreadCount: top-level messages above the caller's read floor, not their own,
capped at 99 -- from dense top-level ordinals (META.topCount / MSG.topOrdinal /
READ.topOrdinal / MEMBER.joinedTopOrdinal), no counting query. mentionCount:
MENTION#<user>#<seq> rows above the floor, thread replies included.
"""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    _member,
    _room,
    repo,
    table,
)

ANN = {"kind": "human", "id": USER_A, "displayName": "Ann"}
BOB = {"kind": "human", "id": USER_B, "displayName": "Bob"}
AGENT = {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


async def _two_humans():
    room_id, _ = await _room(
        members=[_member("human", USER_A, "owner"), _member("human", USER_B), _member("agent", FRIDAY, handle="friday")]
    )
    return room_id


_ids = iter(range(10**6))


async def _say(room_id, author, text="hi", thread_root=None, mentions=()):
    return (
        await repo.create_message(
            room_id=room_id,
            owner_id=OWNER,
            idemp_pk=repo.room_pk(room_id),
            idemp_sk=repo.human_idemp_sk(author["id"], f"c-{next(_ids)}"),
            req_hash=repo.request_hash(text),
            author=author,
            text=text,
            mentions=[{"kind": "human", "id": uid} for uid in mentions],
            reply_to_message_id=None,
            thread_root_message_id=thread_root,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
        )
    )[0]


async def _counts(room_id, user_id):
    rooms = (await messaging.list_rooms(AuthContext(user_id=user_id, org_id=OWNER))).rooms
    room = next(r for r in rooms if str(r.room_id) == room_id)
    dumped = room.model_dump(mode="json", by_alias=True)
    return dumped["unreadCount"], dumped["mentionCount"]


@pytest.mark.asyncio
async def test_own_messages_are_excluded_because_posting_marks_the_room_read(table):
    room_id = await _two_humans()
    await _say(room_id, BOB, "one")
    await _say(room_id, AGENT, "two")
    await _say(room_id, ANN, "three")
    assert await _counts(room_id, USER_A) == (0, 0)
    assert await repo.get_read_cursor(room_id, USER_A) == 3
    assert await _counts(room_id, USER_B) == (2, 0)  # Friday's and Ann's, never Bob's own "one"
    await _say(room_id, AGENT, "four")
    assert await _counts(room_id, USER_A) == (1, 0)


@pytest.mark.asyncio
async def test_thread_replies_count_as_mentions_but_never_as_unread(table):
    room_id = await _two_humans()
    root = await _say(room_id, BOB, "root")
    await repo.set_read_cursor(room_id, USER_A, 1)
    await _say(room_id, BOB, "@ann look", thread_root=root["messageId"], mentions=[USER_A])
    await _say(room_id, AGENT, "plain reply", thread_root=root["messageId"])
    assert await _counts(room_id, USER_A) == (0, 1)
    # A thread reply by the reader does not mark the room read.
    await _say(room_id, ANN, "my reply", thread_root=root["messageId"])
    assert await _counts(room_id, USER_A) == (0, 1)
    # A self-mention is not a mention.
    await _say(room_id, ANN, "@ann note to self", mentions=[USER_A])
    assert await _counts(room_id, USER_A) == (0, 0)


@pytest.mark.asyncio
async def test_mark_read_resets_and_converts_seq_to_the_right_ordinal(table):
    room_id = await _two_humans()
    root = await _say(room_id, BOB, "1")  # ordinal 1
    await _say(room_id, BOB, "2", thread_root=root["messageId"], mentions=[USER_A])  # reply, ordinal 1
    await _say(room_id, BOB, "3", mentions=[USER_A])  # ordinal 2
    await _say(room_id, BOB, "4")  # ordinal 3
    assert await _counts(room_id, USER_A) == (3, 2)
    await repo.set_read_cursor(room_id, USER_A, 2)  # a reply: ordinal 1
    assert await _counts(room_id, USER_A) == (2, 1)
    await repo.set_read_cursor(room_id, USER_A, 4)
    assert await _counts(room_id, USER_A) == (0, 0)
    await repo.mark_unread(room_id, USER_A, joined_seq=0, from_seq=3)
    assert await _counts(room_id, USER_A) == (2, 1)
    await repo.set_read_cursor(room_id, USER_A, 4)
    await repo.mark_unread(room_id, USER_A, joined_seq=0)
    assert await _counts(room_id, USER_A) == (1, 0)


@pytest.mark.asyncio
async def test_counts_cap_at_99(table):
    room_id = await _two_humans()
    await _say(room_id, BOB, "x", mentions=[USER_A])
    for seq in range(2, 120):
        table.put_item(Item={"pk": repo.room_pk(room_id), "sk": repo.mention_sk(USER_A, seq)})
    meta = await repo.get_room(room_id)
    assert repo.unread_count({**meta, "topCount": 500, "lastSeq": 500}, {"joinedSeq": 0}, None) == 99
    assert (await _counts(room_id, USER_A))[1] == 99


@pytest.mark.asyncio
async def test_a_member_joining_later_counts_only_what_came_after(table):
    room_id = await _two_humans()
    await _say(room_id, BOB, "before")
    await _say(room_id, BOB, "before 2")
    meta = await repo.upsert_member(room_id=room_id, owner_id=OWNER, member=_member("human", "user_C"))
    assert meta
    await _say(room_id, BOB, "after", mentions=["user_C"])
    assert await _counts(room_id, "user_C") == (1, 1)


def _strip_ordinals(table, room_id):
    """Rows as written before this PR deployed: no topCount / topOrdinal / joinedTopOrdinal."""
    pk = repo.room_pk(room_id)
    for item in table.scan()["Items"]:
        if item["pk"] != pk:
            continue
        if item["sk"] == "META":
            table.update_item(Key={"pk": pk, "sk": "META"}, UpdateExpression="REMOVE topCount")
        elif item["sk"].startswith(("MSG#", "READ#", "MEMBER#")):
            table.update_item(Key={"pk": pk, "sk": item["sk"]}, UpdateExpression="REMOVE topOrdinal, joinedTopOrdinal")


@pytest.mark.asyncio
async def test_an_unbackfilled_room_reads_as_zero_unread_never_negative(table):
    room_id = await _two_humans()
    await _say(room_id, BOB, "1")
    await _say(room_id, AGENT, "2")
    await repo.set_read_cursor(room_id, USER_A, 1)
    _strip_ordinals(table, room_id)
    assert (await _counts(room_id, USER_A))[0] == 0
    assert (await _counts(room_id, USER_B))[0] == 0
    # A reader whose cursor carries an ordinal above a room's missing topCount clamps at 0.
    meta = await repo.get_room(room_id)
    assert repo.unread_count(meta, {"joinedSeq": 0}, {"lastReadSeq": 2, "topOrdinal": 5}) == 0


@pytest.mark.asyncio
async def test_a_read_room_runs_no_mention_query(table, monkeypatch):
    room_id = await _two_humans()
    await _say(room_id, BOB, "hi", mentions=[USER_A])
    await repo.set_read_cursor(room_id, USER_A, 1)
    spy = AsyncMock(return_value=0)
    monkeypatch.setattr(repo, "mention_count", spy)
    assert await _counts(room_id, USER_A) == (0, 0)
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_send_racing_another_retries_on_fresh_meta_so_ordinals_stay_dense(table, monkeypatch):
    # moto is not thread-safe under concurrent transactions, so the race is forced:
    # the second writer reads META before the first commits.
    room_id = await _two_humans()
    await _say(room_id, AGENT, "root")
    stale = await repo.get_room(room_id)
    await _say(room_id, AGENT, "x")
    real_get_room = repo.get_room
    reads = iter([stale])

    async def get_room(rid):
        return next(reads, None) or await real_get_room(rid)

    monkeypatch.setattr(repo, "get_room", get_room)
    await _say(room_id, BOB, "y", mentions=[USER_A])
    meta = await real_get_room(room_id)
    assert meta["lastSeq"] == 3 and meta["topCount"] == 3
    assert [(await repo.get_message(room_id, seq))["topOrdinal"] for seq in (1, 2, 3)] == [1, 2, 3]
    # The cancelled attempt wrote nothing: one mention row, at the committed seq.
    mentions = [i["sk"] for i in table.scan()["Items"] if i["sk"].startswith("MENTION#")]
    assert mentions == [repo.mention_sk(USER_A, 3)]
    assert (await repo.get_read_row(room_id, USER_B))["topOrdinal"] == 3
    assert await _counts(room_id, USER_A) == (3, 1)


@pytest.mark.asyncio
async def test_a_mention_message_stays_well_inside_the_transaction_limit(table, monkeypatch):
    members = [_member("human", USER_A, "owner")] + [_member("human", f"user_{i}") for i in range(31)]
    room_id, _ = await _room(members=members)
    seen = []
    real = repo._transact

    async def spy(items):
        seen.append(len(items))
        await real(items)

    monkeypatch.setattr(repo, "_transact", spy)
    await _say(room_id, ANN, "all hands", mentions=[f"user_{i}" for i in range(31)])
    assert seen == [4 + 31 + 1]
