"""Canonical messaging table — transaction shapes and ordering guarantees."""

import asyncio
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.repositories import messaging_repo as repo

OWNER = "org_1"
USER_A = "user_A"
USER_B = "user_B"
FRIDAY = "11111111-1111-4111-8111-111111111111"
GAUGE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName="test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "memberKey", "AttributeType": "S"},
                {"AttributeName": "memberSort", "AttributeType": "S"},
                {"AttributeName": "sweep", "AttributeType": "S"},
                {"AttributeName": "deadlineAt", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-member",
                    "KeySchema": [
                        {"AttributeName": "memberKey", "KeyType": "HASH"},
                        {"AttributeName": "memberSort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "by-run-deadline",
                    "KeySchema": [
                        {"AttributeName": "sweep", "KeyType": "HASH"},
                        {"AttributeName": "deadlineAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(repo.settings, "MESSAGING_TABLE", "test-messaging"),
        ):
            yield t


def _member(kind, mid, role="member", listen="active", name=None, handle=None):
    return {
        "kind": kind,
        "id": mid,
        "role": role,
        "listenMode": listen if kind == "agent" else None,
        "displayName": name or mid,
        "mentionHandle": handle,
    }


async def _room(members=None, client_room_id="c-room-1", name="launch", kind="channel"):
    members = members or [_member("human", USER_A, "owner"), _member("agent", FRIDAY, handle="friday")]
    room_id = repo.deterministic_room_id(OWNER, USER_A, client_room_id)
    meta = await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=client_room_id,
        creator_user_id=USER_A,
        name=name,
        kind=kind,
        members=members,
        req_hash=repo.request_hash(name, kind, members),
    )
    return room_id, meta


async def _post(
    room_id,
    text="hi",
    targets=None,
    client_id="m-1",
    author=None,
    thread_root=None,
    mentions=None,
    acting_human_id=None,
):
    """The message only — ``_post_write`` keeps the replay flag."""
    return (
        await _post_write(
            room_id,
            text,
            targets,
            client_id,
            author,
            thread_root,
            mentions,
            acting_human_id,
        )
    )[0]


async def _post_write(
    room_id,
    text="hi",
    targets=None,
    client_id="m-1",
    author=None,
    thread_root=None,
    mentions=None,
    acting_human_id=None,
):
    author = author or {"kind": "human", "id": USER_A, "displayName": "Ann"}
    return await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.room_pk(room_id),
        idemp_sk=repo.human_idemp_sk(USER_A, client_id),
        req_hash=repo.request_hash(text),
        author=author,
        text=text,
        mentions=mentions or [],
        reply_to_message_id=None,
        thread_root_message_id=thread_root,
        run_id=None,
        dispatch_targets=targets if targets is not None else [{"agentId": FRIDAY, "eventKind": "room_event"}],
        source_agent_id=None,
        client_message_id=client_id,
        acting_human_id=acting_human_id,
    )


@pytest.mark.asyncio
async def test_message_edits_keep_identity_work_and_receipts_and_delete_to_tombstone(table):
    from core.services.messaging import canonical_message
    from core.services.openclaw_work import _public_message

    room_id, _ = await _room()
    original = await _post(room_id)
    before = table.scan()["Items"]
    edited = await repo.update_message(OWNER, room_id, 1, USER_A, "edit-1", 0, "corrected")
    assert canonical_message(edited).text == "corrected"
    assert edited["text"] == original["text"]
    assert edited["messageId"] == original["messageId"] and edited["messageSeq"] == 1
    assert (await repo.get_room(room_id))["lastSeq"] == 1
    assert await repo.update_message(OWNER, room_id, 1, USER_A, "edit-1", 0, "corrected") == edited
    with pytest.raises(repo.IdempotencyConflict):
        await repo.update_message(OWNER, room_id, 1, USER_A, "edit-1", 0, "different")
    with pytest.raises(repo.MessageConflict):
        await repo.update_message(OWNER, room_id, 1, USER_A, "stale", 0, "stale overwrite")
    deleted = await repo.update_message(OWNER, room_id, 1, USER_A, "delete", 1, None)
    public = canonical_message(deleted).model_dump(by_alias=True)
    assert (
        public["text"] == "" and public["attachments"] == [] and public["mentions"] == [] and public["reactions"] == {}
    )
    assert public["messageRevision"] == 2 and public["deletedAt"]
    assert _public_message(deleted)["text"] == original["text"]
    assert set(_public_message(deleted)) == {
        "messageId",
        "roomId",
        "messageSeq",
        "roomRevision",
        "author",
        "text",
        "mentions",
        "replyToMessageId",
        "threadRootMessageId",
        "createdAt",
        "runId",
    }
    with pytest.raises(repo.MessageConflict):
        await repo.update_message(OWNER, room_id, 1, USER_A, "undelete", 2, "bring back")
    with pytest.raises(repo.MessagingRepoError):
        await repo.set_reaction(OWNER, room_id, 1, USER_A, "👍", True)
    assert [row for row in table.scan()["Items"] if row["sk"].startswith("WORK#")] == [
        row for row in before if row["sk"].startswith("WORK#")
    ]
    # Old operation retries return the current tombstone, never old content.
    assert (await repo.update_message(OWNER, room_id, 1, USER_A, "edit-1", 0, "corrected"))["deletedAt"]


@pytest.mark.asyncio
async def test_message_edit_author_tenant_membership_and_concurrent_revision_fences(table, monkeypatch):
    room_id, _ = await _room([_member("human", USER_A, "owner"), _member("human", USER_B, "admin")])
    await _post(room_id)
    for tenant, user in [("org_other", USER_A), (OWNER, USER_B), (OWNER, "removed")]:
        with pytest.raises(repo.MessageForbidden):
            await repo.update_message(tenant, room_id, 1, user, "edit", 0, "denied")
    # Moto does not serialize racing conditional transactions like DynamoDB.
    # Commit the competing edit first, then submit this caller's stale real
    # transaction. Moto does enforce its revision condition in this sequence.
    real_transact = repo._transact
    conflict_once = True

    async def transact_after_competing_commit(items):
        nonlocal conflict_once
        if conflict_once:
            conflict_once = False
            await repo.update_message(OWNER, room_id, 1, USER_A, "two", 0, "two")
        await real_transact(items)

    monkeypatch.setattr(repo, "_transact", transact_after_competing_commit)
    with pytest.raises(repo.MessageConflict):
        await repo.update_message(OWNER, room_id, 1, USER_A, "one", 0, "one")
    assert (await repo.get_message(room_id, 1))["editedText"] == "two"
    table.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("human", USER_A)})
    with pytest.raises(repo.MessageForbidden):
        await repo.update_message(OWNER, room_id, 1, USER_A, "retry", 1, None)


@pytest.mark.asyncio
async def test_reactions_persist_idempotently_and_enforce_membership_and_owner(table, monkeypatch):
    room_id, _ = await _room([_member("human", USER_A, "owner"), _member("human", USER_B)])
    message = await _post(room_id)
    seq = message["messageSeq"]
    await repo.set_reaction(OWNER, room_id, seq, USER_A, "👍", True)
    await repo.set_reaction(OWNER, room_id, seq, USER_A, "👍", True)
    updated = await repo.set_reaction(OWNER, room_id, seq, USER_B, "👍", True)
    assert updated["reactions"] == {"👍": [USER_A, USER_B]}
    assert (await repo.get_message(room_id, seq))["reactions"] == updated["reactions"]
    for owner_id, user_id in [("other-owner", USER_A), (OWNER, "outsider")]:
        with pytest.raises(repo.MessagingRepoError):
            await repo.set_reaction(owner_id, room_id, seq, user_id, "❤️", True)
    updated = await repo.set_reaction(OWNER, room_id, seq, USER_A, "👍", False)
    assert updated["reactions"] == {"👍": [USER_B]}
    updated = await repo.set_reaction(OWNER, room_id, seq, USER_B, "👍", False)
    assert updated["reactions"] == {}
    assert updated["messageSeq"] == seq
    # Do not race these callers against Moto: unlike DynamoDB, Moto does not
    # serialize concurrent conditional transactions. Commit the competing
    # reaction first, then submit this caller's stale real transaction. Moto
    # enforces the stale revision condition in that sequence; the retry must
    # reload the committed reaction and merge it.
    real_transact = repo._transact
    conflict_once = True

    async def transact_with_competing_commit(items):
        nonlocal conflict_once
        if conflict_once:
            conflict_once = False
            await repo.set_reaction(OWNER, room_id, seq, USER_A, "❤️", True)
        await real_transact(items)

    monkeypatch.setattr(repo, "_transact", transact_with_competing_commit)
    await repo.set_reaction(OWNER, room_id, seq, USER_B, "❤️", True)
    assert (await repo.get_message(room_id, seq))["reactions"] == {"❤️": [USER_A, USER_B]}


@pytest.mark.asyncio
async def test_create_room_writes_meta_catalog_idemp_members_and_agent_cursors(table):
    room_id, meta = await _room()
    assert meta["lastSeq"] == 0 and meta["revision"] == 1 and meta["deliveryKey"]
    items = table.scan()["Items"]
    sks = sorted(i["sk"] for i in items if i["pk"] == f"ROOM#{room_id}")
    assert sks == sorted(
        [
            "META",
            f"MEMBER#human#{USER_A}",
            f"MEMBER#agent#{FRIDAY}",
            f"IDEMP#human#{USER_A}#c-room-1",
            f"DELIVERY#AGENT#{FRIDAY}",
        ]
    )
    assert any(i["pk"] == f"OWNER#{OWNER}" and i["sk"] == f"ROOM#{room_id}" for i in items)
    member = await repo.get_member(room_id, "agent", FRIDAY)
    assert member["memberKey"] == f"OWNER#{OWNER}#agent#{FRIDAY}" and member["mentionHandle"] == "friday"
    assert (await repo.get_agent_delivery(room_id, FRIDAY))["lastSeq"] == 0


@pytest.mark.asyncio
async def test_create_room_replay_is_idempotent_and_conflict_is_detected(table):
    room_id, meta = await _room()
    room_id2, meta2 = await _room()
    assert room_id2 == room_id and meta2["deliveryKey"] == meta["deliveryKey"]
    with pytest.raises(repo.IdempotencyConflict):
        await _room(name="different")


async def _dm(client_room_id, creator=USER_A, other=("agent", FRIDAY), replaces=None):
    members = [_member("human", creator, "owner"), _member(other[0], other[1])]
    room_id = repo.deterministic_room_id(OWNER, creator, client_room_id)
    return await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=client_room_id,
        creator_user_id=creator,
        name=None,
        kind="dm",
        members=members,
        req_hash=repo.request_hash(None, "dm", members),
        dm_pair=repo.dm_pair_sk(("human", creator), other),
        dm_pair_replaces=replaces,
    )


@pytest.mark.asyncio
async def test_concurrent_dm_creates_for_one_pair_make_one_room(table, monkeypatch):
    # DynamoDB applies each call (a transaction and its conditions included) atomically; moto's
    # in-memory table is not thread-safe, so two creates could both pass `attribute_not_exists`
    # (2 rooms) or a read could hit a write mid-update ("dictionary changed size during
    # iteration") -- flakes prod cannot have. One call at a time, as DynamoDB serializes them;
    # the four creates still interleave between calls, which is the race under test.
    atomic = threading.Lock()
    real = repo.run_in_thread

    async def run_in_thread(call, *args, **kwargs):
        def locked(*a, **kw):
            with atomic:
                return call(*a, **kw)

        return await real(locked, *args, **kwargs)

    monkeypatch.setattr(repo, "run_in_thread", run_in_thread)
    results = await asyncio.gather(*(_dm(f"c-dm-{i}") for i in range(4)), return_exceptions=True)
    created = [r for r in results if isinstance(r, dict)]
    taken = [r for r in results if isinstance(r, repo.DmPairTaken)]
    assert len(created) == 1 and len(taken) == 3, results
    assert {t.room_id for t in taken} == {created[0]["roomId"]}
    assert repo.dm_pair_sk(("agent", FRIDAY), ("human", USER_A)) == repo.dm_pair_sk(
        ("human", USER_A), ("agent", FRIDAY)
    )
    # The same request replayed still returns its own room, not DmPairTaken.
    assert (await _dm(f"c-dm-{results.index(created[0])}"))["roomId"] == created[0]["roomId"]
    # Another pair is unaffected; a released pair can be taken again.
    await _dm("c-dm-b", creator=USER_B)
    # Only a create naming the row's current (stale) room may take it over.
    with pytest.raises(repo.DmPairTaken):
        await _dm("c-dm-again", replaces="some-other-room")
    assert (await _dm("c-dm-again", replaces=created[0]["roomId"]))["roomId"] != created[0]["roomId"]


@pytest.mark.asyncio
async def test_member_limit_and_action_formula(table):
    members = [_member("human", USER_A, "owner")] + [
        _member("agent", f"{i:08d}-0000-4000-8000-000000000000") for i in range(32)
    ]
    with pytest.raises(repo.MemberLimitExceeded):
        await _room(members=members)
    members = members[:32]  # 1 human + 31 agents == 32 members, 66 actions
    room_id, _ = await _room(members=members, client_room_id="c-room-32")
    assert len(await repo.list_members(room_id)) == 32


@pytest.mark.asyncio
async def test_messages_use_20_digit_seq_and_page_in_order(table):
    room_id, _ = await _room()
    for i in range(3):
        m = await _post(room_id, text=f"t{i}", client_id=f"m-{i}")
        assert m["messageSeq"] == i + 1 and m["sk"] == f"MSG#{i + 1:020d}"
    page, nxt = await repo.list_messages(room_id, after_seq=0, limit=2)
    assert [m["text"] for m in page] == ["t0", "t1"] and nxt == 2
    page, nxt = await repo.list_messages(room_id, after_seq=2, limit=2)
    assert [m["text"] for m in page] == ["t2"] and nxt is None
    assert (await repo.get_room(room_id))["lastSeq"] == 3
    with pytest.raises(ValueError):
        await repo.list_messages(room_id, 0, 101)


async def test_get_messages_by_keys_returns_rows_and_omits_missing(table):
    room_id, _ = await _room()
    await _post(room_id, text="t0", client_id="m-0")
    got = await repo.get_messages_by_keys([(room_id, 1), (room_id, 99), (room_id, 1)])
    assert list(got) == [(room_id, 1)] and got[(room_id, 1)]["text"] == "t0"
    assert await repo.get_messages_by_keys([]) == {}


async def test_list_thread_returns_root_and_replies_before_seq_in_order(table):
    room_id, _ = await _room()
    root = await _post(room_id, text="root", client_id="m-0")
    await _post(room_id, text="other", client_id="m-1")
    await _post(room_id, text="r1", client_id="m-2", thread_root=root["messageId"])
    await _post(room_id, text="r2", client_id="m-3", thread_root=root["messageId"])
    thread = await repo.list_thread(room_id, root["messageId"], 4)
    assert [m["text"] for m in thread] == ["root", "r1"]
    assert await repo.list_thread(room_id, root["messageId"], 1) == []


async def test_list_thread_reads_a_bounded_window_newest_first(table, monkeypatch):
    # Bounded: a room's whole history must never be scanned once per candidate agent.
    room_id, _ = await _room()
    root = await _post(room_id, text="root", client_id="b-0")
    await _post(room_id, text="r1", client_id="b-1", thread_root=root["messageId"])
    monkeypatch.setattr(repo, "_THREAD_SCAN_LIMIT", 1)
    assert [m["text"] for m in await repo.list_thread(room_id, root["messageId"], 3)] == ["r1"]


@pytest.mark.asyncio
async def test_newest_page_and_older_cursor_are_exclusive(table):
    room_id, _ = await _room()
    for i in range(5):
        await _post(room_id, text=f"t{i}", client_id=f"newest-{i}")
    page, before = await repo.list_messages_before_page(room_id, 6, 2)
    assert [m["messageSeq"] for m in page] == [4, 5] and before == 4
    page, before = await repo.list_messages_before_page(room_id, before, 2)
    assert [m["messageSeq"] for m in page] == [2, 3] and before == 2
    page, before = await repo.list_messages_before_page(room_id, before, 2)
    assert [m["messageSeq"] for m in page] == [1] and before is None
    assert await repo.list_messages_before_page(room_id, 1, 2) == ([], None)
    with pytest.raises(ValueError):
        await repo.list_messages_before_page(room_id, 6, 101)


@pytest.mark.asyncio
async def test_message_transaction_writes_idemp_and_one_work_row_per_target(table):
    room_id, _ = await _room(
        members=[_member("human", USER_A, "owner"), _member("agent", FRIDAY), _member("agent", GAUGE)]
    )
    m = await _post(
        room_id,
        targets=[{"agentId": FRIDAY, "eventKind": "user_request"}, {"agentId": GAUGE, "eventKind": "room_event"}],
    )
    items = {i["sk"]: i for i in table.scan()["Items"] if i["pk"] == f"ROOM#{room_id}"}
    work = [v for k, v in items.items() if k.startswith("WORK#MESSAGE#")]
    assert sorted(i["sk"] for i in work) == [
        f"WORK#MESSAGE#{1:020d}#AGENT#{FRIDAY}",
        f"WORK#MESSAGE#{1:020d}#AGENT#{GAUGE}",
    ]
    friday = next(w for w in work if w["payload"]["agentId"] == FRIDAY)
    assert friday["payload"] == {"kind": "room_dispatch", "roomId": room_id, "agentId": FRIDAY, "throughSeq": 1}
    group, dedup = repo.room_fifo_ids(OWNER, room_id, FRIDAY, m["messageId"])
    assert friday["messageGroupId"] == group and friday["messageDeduplicationId"] == dedup
    assert len(group) == 64 and len(dedup) == 64
    assert abs(int(friday["expiresAt"]) - (time.time() + 7 * 86400)) < 60
    receipt = items[f"IDEMP#human#{USER_A}#m-1"]
    assert receipt["messageId"] == m["messageId"] and receipt["messageSeq"] == 1


@pytest.mark.asyncio
async def test_message_replay_returns_original_and_changed_body_conflicts(table):
    room_id, _ = await _room()
    first, first_replayed = await _post_write(room_id, text="same")
    again, again_replayed = await _post_write(room_id, text="same")
    assert again["messageId"] == first["messageId"] and (await repo.get_room(room_id))["lastSeq"] == 1
    # The flag is what lets a caller skip post-commit side effects on a retry.
    assert (first_replayed, again_replayed) == (False, True)
    with pytest.raises(repo.IdempotencyConflict):
        await _post(room_id, text="different")


@pytest.mark.asyncio
async def test_create_message_refuses_a_human_author_with_no_member_row(table):
    """A sender who lost their MEMBER row before this transaction committed (e.g. removed
    mid-request, after the caller's own require_room_member pre-check passed) must not have
    their message land. USER_B is never added to the room."""
    room_id, _ = await _room()
    with pytest.raises(repo.MessageForbidden):
        await _post(room_id, author={"kind": "human", "id": USER_B, "displayName": "Bea"}, targets=[])
    assert (await repo.get_room(room_id))["lastSeq"] == 0  # nothing committed


@pytest.mark.asyncio
async def test_create_message_refuses_an_agent_author_with_no_member_row(table):
    """Same guard for the agent path -- GAUGE is a valid agent id but never added to this
    room, standing in for one removed between the caller's get_member pre-check and commit."""
    room_id, _ = await _room()
    with pytest.raises(repo.MessageForbidden):
        await repo.create_message(
            room_id=room_id,
            owner_id=OWNER,
            idemp_pk=repo.owner_pk(OWNER),
            idemp_sk=repo.agent_idemp_sk("evt-1"),
            req_hash=repo.request_hash("hi"),
            author={"kind": "agent", "id": GAUGE, "displayName": "Gauge"},
            text="hi",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=GAUGE,
            client_message_id=None,
        )
    assert (await repo.get_room(room_id))["lastSeq"] == 0  # nothing committed


@pytest.mark.asyncio
async def test_agent_receipt_is_owner_scoped_by_event_id(table):
    room_id, _ = await _room()
    author = {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}
    m, _ = await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.owner_pk(OWNER),
        idemp_sk=repo.agent_idemp_sk("q-1:0"),
        req_hash="h",
        author=author,
        text="reply",
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=FRIDAY,
        client_message_id=None,
    )
    receipt = await repo.get_agent_idempotency(OWNER, "q-1:0")
    assert receipt["messageId"] == m["messageId"] and receipt["roomId"] == room_id
    assert await repo.get_agent_idempotency("org_other", "q-1:0") is None


@pytest.mark.asyncio
async def test_sequence_conflict_is_retried(table, monkeypatch):
    # The racer stands for a writer on ANOTHER backend task: no shared in-process lock.
    monkeypatch.setattr(repo, "_room_append_lock", lambda _rid: asyncio.Lock())
    room_id, _ = await _room()
    real_get_room = repo.get_room
    calls = {"n": 0}

    async def stale_then_fresh(rid):
        meta = await real_get_room(rid)
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a writer landing between our read and our transaction.
            await _post(rid, text="racer", client_id="racer")
        return meta

    with patch.object(repo, "get_room", stale_then_fresh):
        m = await _post(room_id, text="mine", client_id="mine")
    assert m["messageSeq"] == 2 and calls["n"] >= 2


@pytest.mark.asyncio
async def test_sequence_conflicts_back_off_with_growing_jitter_then_give_up(table, monkeypatch):
    monkeypatch.setattr(repo, "_room_append_lock", lambda _rid: asyncio.Lock())  # racers on another task
    room_id, _ = await _room()
    real_get_room = repo.get_room

    racing = {"on": False}

    async def always_stale(rid):
        meta = await real_get_room(rid)
        if not racing["on"]:
            # A racer lands between every read of ours and our transaction.
            racing["on"] = True
            await _post(rid, text="racer", client_id=f"racer-{uuid.uuid4()}")
            racing["on"] = False
        return meta

    bounds, sleeps = [], []
    monkeypatch.setattr(repo.random, "uniform", lambda lo, hi: bounds.append(hi) or hi)
    monkeypatch.setattr(repo.asyncio, "sleep", AsyncMock(side_effect=lambda s: sleeps.append(s)))
    with patch.object(repo, "get_room", always_stale), pytest.raises(repo.MessagingRepoError, match="after 8 attempts"):
        await _post(room_id, text="mine", client_id="mine")
    # One sleep between each pair of attempts, none after the last; bounds double up to the cap.
    assert len(sleeps) == repo._SEQ_RETRY_ATTEMPTS - 1
    assert bounds == [min(repo._SEQ_BACKOFF_CAP_S, repo._SEQ_BACKOFF_BASE_S * 2**n) for n in range(len(sleeps))]
    assert bounds == sorted(bounds) and bounds[0] < bounds[-1] and sum(bounds) < 2


@pytest.mark.asyncio
async def test_concurrent_appends_to_one_room_in_one_process_take_turns_without_conflicts(table, monkeypatch):
    room_id, _ = await _room()
    real_get_room = repo.get_room
    reads = []

    async def counted(rid):
        reads.append(rid)
        return await real_get_room(rid)

    sleep = AsyncMock()
    monkeypatch.setattr(repo.asyncio, "sleep", sleep)
    with patch.object(repo, "get_room", counted):
        posted = await asyncio.gather(*(_post(room_id, text=f"m{n}", client_id=f"c-{n}") for n in range(16)))
    assert sorted(m["messageSeq"] for m in posted) == list(range(1, 17))
    # One META read per append and no backoff: nobody lost a lastSeq race.
    assert len(reads) == 16 and sleep.await_count == 0


@pytest.mark.asyncio
async def test_gsi_is_discovery_only_and_base_membership_is_strong(table):
    room_id, _ = await _room()
    found = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert [f["meta"]["roomId"] for f in found] == [room_id]
    assert await repo.list_rooms_for_member("org_other", "human", USER_A) == []
    # A stale GSI entry can never pass the base read.
    meta = await repo.get_room(room_id)
    await repo.remove_member(meta=meta, kind="human", member_id=USER_A, new_delivery_key="k2")
    assert await repo.get_member(room_id, "human", USER_A) is None


@pytest.mark.asyncio
async def test_owner_removal_promotes_the_heir_in_the_same_transaction(table):
    room_id, meta = await _room(members=[_member("human", USER_A, "owner"), _member("human", USER_B)])
    await repo.remove_member(meta=meta, kind="human", member_id=USER_A, new_delivery_key="k2", new_owner_id=USER_B)
    assert await repo.get_member(room_id, "human", USER_A) is None
    assert (await repo.get_member(room_id, "human", USER_B))["role"] == "owner"


@pytest.mark.asyncio
async def test_human_removal_rotates_key_and_writes_live_rows_transactionally(table):
    room_id, meta = await _room(
        members=[_member("human", USER_A, "owner"), _member("human", USER_B), _member("agent", FRIDAY)]
    )
    key_a = await repo.ensure_user_stream(OWNER, USER_A)
    old = meta["deliveryKey"]
    live = [
        {
            "userId": USER_A,
            "channelKey": key_a,
            "event": {"eventId": "e1", "type": "room.subscription.rotated", "payload": {}},
        },
        {
            "userId": USER_B,
            "channelKey": None,
            "event": {"eventId": "e2", "type": "room.access.removed", "payload": {}},
        },
    ]
    new_meta = await repo.remove_member(
        meta=meta,
        kind="human",
        member_id=USER_B,
        new_delivery_key="00000000-0000-4000-8000-000000000099",
        live_events=live,
    )
    assert new_meta["deliveryKey"] != old and new_meta["revision"] == 2
    assert (await repo.get_room(room_id))["deliveryKey"] == new_meta["deliveryKey"]
    rows = {i["sk"]: i for i in table.scan()["Items"] if i["pk"] == f"ROOM#{room_id}"}
    assert rows[f"LIVE#e1#USER#{USER_A}"]["channelKey"] == key_a
    assert rows[f"LIVE#e2#USER#{USER_B}"]["event"]["type"] == "room.access.removed"
    assert all(rows[k]["expiresAt"] > time.time() + 6 * 86400 for k in rows if k.startswith("LIVE#"))
    assert f"MEMBER#human#{USER_B}" not in rows
    # A second removal of the same member fails closed (nothing to delete) and the key stays.
    with pytest.raises(Exception):
        await repo.remove_member(meta=new_meta, kind="human", member_id=USER_B, new_delivery_key="again")
    assert (await repo.get_room(room_id))["deliveryKey"] == new_meta["deliveryKey"]
    # A stale META (old revision) is refused, so a notice built for the wrong revision never lands.
    with pytest.raises(Exception):
        await repo.remove_member(meta=meta, kind="agent", member_id=FRIDAY)


@pytest.mark.asyncio
async def test_agent_added_later_starts_its_cursor_at_current_seq(table):
    room_id, _ = await _room()
    for i in range(4):
        await _post(room_id, text=f"t{i}", client_id=f"m-{i}")
    await repo.upsert_member(
        room_id=room_id,
        owner_id=OWNER,
        member=_member("agent", GAUGE, handle="gauge"),
        handle_updates={FRIDAY: "friday"},
    )
    assert (await repo.get_agent_delivery(room_id, GAUGE))["lastSeq"] == 4
    assert (await repo.get_member(room_id, "agent", GAUGE))["joinedSeq"] == 4
    assert (await repo.get_room(room_id))["revision"] == 2
    # Agent removal drops its cursor and does NOT rotate the room key.
    before = (await repo.get_room(room_id))["deliveryKey"]
    await repo.remove_member(meta=await repo.get_room(room_id), kind="agent", member_id=GAUGE)
    assert await repo.get_agent_delivery(room_id, GAUGE) is None
    assert (await repo.get_room(room_id))["deliveryKey"] == before


@pytest.mark.asyncio
async def test_agent_cursor_is_compare_and_set(table):
    room_id, _ = await _room()
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=0, through=3) is True
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=0, through=5) is False  # moved
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=3, through=2) is False  # backward
    assert await repo.advance_agent_delivery(room_id, GAUGE, expected=0, through=1) is False  # absent
    assert (await repo.get_agent_delivery(room_id, FRIDAY))["lastDeliveredSeq"] == 0
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=3, through=4, accepted=True) is True
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=4, through=5) is True
    cursor = await repo.get_agent_delivery(room_id, FRIDAY)
    assert cursor["lastSeq"] == 5 and cursor["lastDeliveredSeq"] == 4


@pytest.mark.asyncio
async def test_read_cursor_is_monotonic_and_bounded(table):
    room_id, _ = await _room()
    await _post(room_id, client_id="m-1")
    await _post(room_id, client_id="m-2")
    assert await repo.set_read_cursor(room_id, USER_A, 9) == 2
    assert await repo.set_read_cursor(room_id, USER_A, 1) == 2
    assert await repo.get_read_cursor(room_id, USER_B) == 0


@pytest.mark.asyncio
async def test_delegated_system_post_marks_accountable_human_read_without_self_mention(table):
    room_id, _ = await _room(
        members=[
            _member("human", USER_A, "owner"),
            _member("human", USER_B),
            _member("agent", FRIDAY, handle="friday"),
        ]
    )
    message = await _post(
        room_id,
        author={"kind": "system", "id": "native-session", "displayName": "Codex session"},
        mentions=[
            {"kind": "human", "id": USER_A, "displayName": "Ann"},
            {"kind": "human", "id": USER_B, "displayName": "Bob"},
        ],
        acting_human_id=USER_A,
    )

    assert await repo.get_read_cursor(room_id, USER_A) == message["messageSeq"]
    assert await repo.mention_count(room_id, USER_A, 0) == 0
    assert await repo.mention_count(room_id, USER_B, 0) == 1


@pytest.mark.asyncio
async def test_mark_unread_only_lowers_cursor_to_one_before_latest(table):
    room_id, _ = await _room()
    for index in range(1, 5):
        await _post(room_id, client_id=f"m-{index}")
    await repo.set_read_cursor(room_id, USER_A, 4)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0) == 3
    await repo.set_read_cursor(room_id, USER_B, 2)
    assert await repo.mark_unread(room_id, USER_B, joined_seq=0) == 2


@pytest.mark.asyncio
async def test_mark_unread_respects_join_floor_and_empty_room(table):
    room_id, _ = await _room()
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0) == 0
    for index in range(1, 5):
        await _post(room_id, client_id=f"floor-{index}")
    await repo.set_read_cursor(room_id, USER_A, 4)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=3) == 3


@pytest.mark.asyncio
async def test_mark_unread_races_with_read_and_new_message_without_clearing_unread(table):
    room_id, _ = await _room()
    for index in range(1, 5):
        await _post(room_id, client_id=f"race-{index}")
    await repo.set_read_cursor(room_id, USER_A, 4)

    await asyncio.gather(
        repo.mark_unread(room_id, USER_A, joined_seq=0),
        repo.set_read_cursor(room_id, USER_A, 4),
        # Someone else's message: Ann's own top-level post would mark the room read for her.
        _post(room_id, client_id="race-new", author={"kind": "agent", "id": FRIDAY, "displayName": "Friday"}),
    )

    cursor = await repo.get_read_cursor(room_id, USER_A)
    assert cursor in {3, 4}
    assert int((await repo.get_room(room_id))["lastSeq"]) - cursor >= 1


@pytest.mark.asyncio
async def test_user_stream_is_stable_then_fresh_after_deactivation(table):
    first = await repo.ensure_user_stream(OWNER, USER_A)
    assert await repo.ensure_user_stream(OWNER, USER_A) == first
    reverse = await repo.get_stream(first)
    assert reverse["userId"] == USER_A and reverse["ownerId"] == OWNER and reverse["active"] is True
    assert await repo.deactivate_user_stream(OWNER, USER_A) is True
    assert await repo.deactivate_user_stream(OWNER, USER_A) is False
    assert (await repo.get_stream(first))["active"] is False  # old channel goes permanently silent
    assert await repo.list_active_user_streams(OWNER) == []
    # A removed member (stale JWT) never revives the stream; only a real rejoin does.
    assert await repo.ensure_user_stream(OWNER, USER_A) is None
    assert (await repo.get_user_stream(OWNER, USER_A))["active"] is False
    second = await repo.ensure_user_stream(OWNER, USER_A, rejoin=True)
    assert second != first and (await repo.get_stream(second))["active"] is True
    assert (await repo.get_stream(first))["active"] is False


@pytest.mark.asyncio
async def test_operation_lifecycle_lease_and_outbox(table):
    row = await repo.create_operation(
        owner_id=OWNER, operation_id="op-1", requester_user_id=USER_A, method="agents.create", params={"name": "Ray"}
    )
    assert row["state"] == "queued"
    work = next(i for i in table.scan()["Items"] if i["sk"] == "WORK#OPERATION#op-1")
    group, dedup = repo.operation_fifo_ids(OWNER, "op-1")
    assert work["payload"] == {"kind": "runtime_operation", "ownerId": OWNER, "operationId": "op-1"}
    assert work["messageGroupId"] == group and work["messageDeduplicationId"] == dedup
    claimed = await repo.claim_operation(OWNER, "op-1", lease_token="t1", lease_until=int(time.time()) + 300)
    assert claimed["state"] == "running"
    assert await repo.claim_operation(OWNER, "op-1", lease_token="t2", lease_until=int(time.time()) + 300) is None
    # Expired lease can be reclaimed by a redelivery.
    table.update_item(
        Key={"pk": f"OWNER#{OWNER}", "sk": "OPERATION#op-1"},
        UpdateExpression="SET leaseUntil = :z",
        ExpressionAttributeValues={":z": 1},
    )
    reclaimed = await repo.claim_operation(OWNER, "op-1", lease_token="t2", lease_until=int(time.time()) + 300)
    assert reclaimed["leaseToken"] == "t2"
    assert (
        await repo.complete_operation(OWNER, "op-1", lease_token="t1", state="succeeded", result={}, error=None) is None
    )
    done = await repo.complete_operation(
        OWNER, "op-1", lease_token="t2", state="succeeded", result={"agentId": "x"}, error=None
    )
    assert done["state"] == "succeeded" and "leaseToken" not in done
    assert await repo.claim_operation(OWNER, "op-1", lease_token="t3", lease_until=int(time.time()) + 300) is None


@pytest.mark.asyncio
async def test_wipe_deletes_room_partitions_owner_rows_and_streams_only_for_that_owner(table):
    room_id, _ = await _room()
    await _post(room_id)
    key = await repo.ensure_user_stream(OWNER, USER_A)
    other_key = await repo.ensure_user_stream("org_other", USER_B)
    for scope in (OWNER, OWNER + "_other"):
        for pk, sk in [
            (f"LOCALUSER#{scope}#user_A", "DEVICE#mac"),
            (f"LOCALSESSION#{scope}#session", "META"),
            (f"LOCALSESSION#{scope}#session", "EVENT#00000000000000000001"),
        ]:
            table.put_item(Item={"pk": pk, "sk": sk})
    assert await repo.list_owner_room_ids(OWNER) == [room_id]
    assert await repo.delete_room_partition(room_id) > 0
    original_scan = table.scan
    # Real filtered pages (including empty pages with continuation keys).
    with (
        patch.object(repo, "_table", return_value=table),
        patch.object(table, "scan", side_effect=lambda **kwargs: original_scan(**{**kwargs, "Limit": 1})),
    ):
        assert await repo.delete_owner_partition(OWNER) > 0
    assert await repo.get_room(room_id) is None and await repo.get_stream(key) is None
    assert await repo.get_stream(other_key) is not None
    remaining = [r for r in table.scan()["Items"] if r["pk"].startswith("LOCAL")]
    assert len(remaining) == 3 and all(f"#{OWNER}_other#" in r["pk"] for r in remaining)
    assert await repo.delete_owner_partition(OWNER) == 0
    assert await repo.delete_room_partition(room_id) == 0  # idempotent retry


@pytest.mark.asyncio
async def test_workspace_wipe_removes_hosted_mcp_send_receipts(table):
    import uuid

    from core.auth import AuthContext
    from core.services import hosted_mcp

    pk, sk = hosted_mcp._sent_key(AuthContext(user_id=USER_A, org_id=OWNER), uuid.uuid4())
    table.put_item(Item={"pk": pk, "sk": sk, "destination": "room:r"})
    assert await repo.delete_owner_partition(OWNER) > 0
    assert await repo._get(pk, sk) is None


@pytest.mark.asyncio
async def test_set_member_prefs_writes_only_named_fields_and_leaves_revision_alone(table):
    room_id, meta = await _room()

    updated = await repo.set_member_prefs(room_id, "human", USER_A, {"starred": True, "sectionId": "sec-1"})
    assert updated["starred"] is True
    assert updated["sectionId"] == "sec-1"
    assert updated.get("muted") in (None, False)

    only_mute = await repo.set_member_prefs(room_id, "human", USER_A, {"muted": True})
    assert only_mute["muted"] is True
    assert only_mute["starred"] is True
    assert only_mute["sectionId"] == "sec-1"

    cleared = await repo.set_member_prefs(room_id, "human", USER_A, {"sectionId": None})
    assert cleared["sectionId"] is None
    assert cleared["starred"] is True

    assert (await repo.get_room(room_id))["revision"] == meta["revision"]
    listed = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert listed[0]["member"]["starred"] is True


@pytest.mark.asyncio
async def test_set_member_prefs_rejects_a_member_that_is_not_in_the_room(table):
    room_id, _ = await _room()
    with pytest.raises(repo.RoomNotFound):
        await repo.set_member_prefs(room_id, "human", USER_B, {"starred": True})


@pytest.mark.asyncio
async def test_user_sections_round_trip_preserve_order_and_replace_wholesale(table):
    assert await repo.get_user_prefs(OWNER, USER_A) is None

    first = await repo.put_user_sections(OWNER, USER_A, [{"id": "s-2", "name": "Ship"}, {"id": "s-1", "name": "Admin"}])
    assert first == [{"id": "s-2", "name": "Ship"}, {"id": "s-1", "name": "Admin"}]
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == first

    replaced = await repo.put_user_sections(OWNER, USER_A, [{"id": "s-1", "name": "Admin"}])
    assert replaced == [{"id": "s-1", "name": "Admin"}]
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == replaced

    assert await repo.put_user_sections(OWNER, USER_A, []) == []


@pytest.mark.asyncio
async def test_user_sections_are_per_user_and_survive_stream_deactivation(table):
    await repo.put_user_sections(OWNER, USER_A, [{"id": "s-1", "name": "Admin"}])
    assert await repo.get_user_prefs(OWNER, USER_B) is None

    await repo.ensure_user_stream(OWNER, USER_A)
    await repo.deactivate_user_stream(OWNER, USER_A)
    assert (await repo.get_user_prefs(OWNER, USER_A))["sections"] == [{"id": "s-1", "name": "Admin"}]


@pytest.mark.asyncio
async def test_put_user_sections_does_not_clobber_other_pref_row_attributes(table):
    repo._table().put_item(
        Item={
            "pk": repo.owner_pk(OWNER),
            "sk": repo.pref_sk(USER_A),
            "ownerId": OWNER,
            "userId": USER_A,
            "theme": "dark",
        }
    )

    await repo.put_user_sections(OWNER, USER_A, [{"id": "s-1", "name": "Admin"}])

    prefs = await repo.get_user_prefs(OWNER, USER_A)
    assert prefs["sections"] == [{"id": "s-1", "name": "Admin"}]
    assert prefs["theme"] == "dark"


@pytest.mark.asyncio
async def test_patch_room_meta_writes_name_topic_and_mirrors_only_the_name(table):
    room_id, meta = await _room(name="Launch crew")

    renamed = await repo.patch_room_meta(room_id, {"name": "Release crew"})
    assert renamed["name"] == "Release crew"
    assert renamed["revision"] == meta["revision"] + 1
    assert (await repo.get_room(room_id))["name"] == "Release crew"
    listed = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert listed[0]["meta"]["name"] == "Release crew"

    topicked = await repo.patch_room_meta(room_id, {"topic": "Ship v2 by Friday"})
    assert topicked["topic"] == "Ship v2 by Friday"
    assert topicked["name"] == "Release crew"
    assert topicked["revision"] == renamed["revision"] + 1

    both = await repo.patch_room_meta(room_id, {"name": None, "topic": None})
    assert both["name"] is None and both["topic"] is None


@pytest.mark.asyncio
async def test_patch_room_meta_rejects_an_unknown_room(table):
    with pytest.raises(repo.RoomNotFound):
        await repo.patch_room_meta("11111111-1111-4111-8111-999999999999", {"topic": "x"})


@pytest.mark.asyncio
async def test_set_pin_appends_removes_is_idempotent_and_caps_at_fifty(table):
    room_id, meta = await _room()

    pinned = await repo.set_pin(room_id, "m-1", True)
    assert pinned["pinnedMessageIds"] == ["m-1"]
    assert pinned["revision"] == meta["revision"] + 1

    again = await repo.set_pin(room_id, "m-1", True)
    assert again["pinnedMessageIds"] == ["m-1"]
    assert again["revision"] == pinned["revision"]

    two = await repo.set_pin(room_id, "m-2", True)
    assert two["pinnedMessageIds"] == ["m-1", "m-2"]

    unpinned = await repo.set_pin(room_id, "m-1", False)
    assert unpinned["pinnedMessageIds"] == ["m-2"]

    missing = await repo.set_pin(room_id, "m-9", False)
    assert missing["pinnedMessageIds"] == ["m-2"]
    assert missing["revision"] == unpinned["revision"]

    for index in range(3, 52):
        await repo.set_pin(room_id, f"m-{index}", True)
    assert len((await repo.get_room(room_id))["pinnedMessageIds"]) == 50
    with pytest.raises(repo.PinLimitExceeded):
        await repo.set_pin(room_id, "m-over", True)


@pytest.mark.asyncio
async def test_noted_rows_list_newest_first_overwrite_on_retry_and_forget(table):
    room_id, _ = await _room()
    await repo.put_noted(room_id, "m-1", "Classes cap at 12 kids.", actor_user_id=USER_A)
    await repo.put_noted(room_id, "m-2", "We only use Caputo flour.")
    await repo.put_noted(room_id, "m-1", "Classes cap at 12 kids.")  # a retried Router turn
    assert [r["messageId"] for r in await repo.list_noted(room_id)] == ["m-2", "m-1"]
    await repo.delete_noted(room_id, "m-2")
    await repo.delete_noted(room_id, "m-2")  # idempotent
    assert [r["fact"] for r in await repo.list_noted(room_id)] == ["Classes cap at 12 kids."]


@pytest.mark.asyncio
async def test_mark_unread_from_a_sequence_leaves_that_message_and_everything_after_unread(table):
    room_id, _ = await _room()
    for index in range(1, 6):
        await _post(room_id, text=f"m{index}", client_id=f"c-{index}")
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.get_read_cursor(room_id, USER_A) == 5

    assert await repo.mark_unread(room_id, USER_A, joined_seq=0, from_seq=3) == 2
    assert await repo.get_read_cursor(room_id, USER_A) == 2

    # Never moves the cursor forward: marking from a later message is a no-op.
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0, from_seq=5) == 2
    # No from_seq keeps the shipped whole-room behaviour, floored at joined_seq.
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=0) == 4
    await repo.set_read_cursor(room_id, USER_A, 5)
    assert await repo.mark_unread(room_id, USER_A, joined_seq=3, from_seq=1) == 3


@pytest.mark.asyncio
async def test_scan_growth_rows_returns_only_messages_and_rooms_without_bodies(table):
    """The admin growth read sees every MSG# row and every ROOM META row — and
    nothing else. ``STREAM#…`` rows also carry sk="META", so the pk prefix is what
    separates a room from a private-channel pointer; message text is never
    projected, because a metrics read has no business carrying message bodies."""
    room_id, _ = await _room()
    await _post(room_id, text="secret plan")
    await repo.ensure_user_stream(OWNER, USER_A)

    messages, rooms = await repo.scan_growth_rows()

    assert [m["roomId"] for m in messages] == [room_id]
    assert messages[0]["author"]["kind"] == "human" and messages[0]["ownerId"] == OWNER
    assert messages[0]["dispatchTargets"] == [{"agentId": FRIDAY, "eventKind": "room_event"}]
    assert messages[0]["createdAt"]
    assert "text" not in messages[0] and "mentions" not in messages[0]
    assert [r["roomId"] for r in rooms] == [room_id]
    assert {r["sk"] for r in rooms} == {"META"}


@pytest.mark.asyncio
async def test_ensure_workspace_id_mints_once_and_is_stamped_on_rooms_and_streams(table):
    first = await repo.ensure_workspace_id(OWNER)
    assert first == await repo.ensure_workspace_id(OWNER)
    assert first != await repo.ensure_workspace_id("owner_other")
    room_id, meta = await _room()  # the file's existing helper around create_room
    assert meta["workspaceId"] == first
    assert (await repo.get_room(room_id))["workspaceId"] == first
    key = await repo.ensure_user_stream(OWNER, USER_A)
    assert (await repo.get_stream(key))["workspaceId"] == first


@pytest.mark.asyncio
async def test_workspace_id_for_meta_returns_the_existing_id_without_writing(table, monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(repo, "ensure_workspace_id", ensure)
    meta = {"ownerId": OWNER, "workspaceId": "existing-ws"}
    assert await repo.workspace_id_for_meta("ROOM#x", "META", meta) == "existing-ws"
    ensure.assert_not_called()
    assert (await repo._get("ROOM#x", "META")) is None


@pytest.mark.asyncio
async def test_workspace_id_for_meta_stamps_and_returns_the_ensured_id_when_missing(table, monkeypatch):
    monkeypatch.setattr(repo, "ensure_workspace_id", AsyncMock(return_value="minted-ws"))
    meta = {"ownerId": OWNER}
    assert await repo.workspace_id_for_meta("ROOM#x", "META", meta) == "minted-ws"
    assert meta["workspaceId"] == "minted-ws"
    assert (await repo._get("ROOM#x", "META"))["workspaceId"] == "minted-ws"


@pytest.mark.asyncio
async def test_workspace_id_for_meta_ignores_a_conditional_check_failed_race(table, monkeypatch):
    monkeypatch.setattr(repo, "ensure_workspace_id", AsyncMock(return_value="minted-ws"))
    monkeypatch.setattr(
        repo,
        "run_in_thread",
        AsyncMock(side_effect=ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")),
    )
    meta = {"ownerId": OWNER}
    assert await repo.workspace_id_for_meta("ROOM#x", "META", meta) == "minted-ws"
    assert meta["workspaceId"] == "minted-ws"


RUN_ROOM = "22222222-2222-4222-8222-222222222222"
USAGE = {
    "model": "claude-sonnet-4-6",
    "inputTokens": 1200,
    "outputTokens": 300,
    "cacheReadTokens": 5000,
    "cacheWriteTokens": 100,
    "estimatedCostUsd": 0.0123,
}


async def _dispatched(owner="owner_1", agent="a1", message="m1"):
    run_id = repo.run_id_for(agent, message)
    created = await repo.create_run(
        owner,
        run_id,
        room_id=RUN_ROOM,
        agent_id=agent,
        workspace_id="ws",
        trigger_message_id=message,
        event_kind="user_request",
    )
    return run_id, created


@pytest.mark.asyncio
async def test_run_id_is_deterministic_and_the_row_is_created_once(table):
    run_id, created = await _dispatched()
    assert created and run_id == repo.run_id_for("a1", "m1")
    assert (await _dispatched())[1] is False
    row = await repo.get_run("owner_1", run_id)
    assert row["status"] == "dispatched" and row["sweep"] == "open" and row["deadlineAt"] > int(time.time())


@pytest.mark.asyncio
async def test_transitions_are_conditional(table):
    run_id, _ = await _dispatched()
    assert await repo.start_run("owner_1", run_id, session_key="agent:a1:lightbulb:channel:x")
    assert not await repo.start_run("owner_1", run_id, session_key="again")
    before = (await repo.get_run("owner_1", run_id))["deadlineAt"]
    assert await repo.heartbeat_run("owner_1", run_id)
    assert (await repo.get_run("owner_1", run_id))["deadlineAt"] >= before
    ended = await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert (
        ended["status"] == "ended"
        and ended["usage"]["inputTokens"] == 1200
        and ended["sweep"] == "followup"
        and ended["deadlineAt"] >= int(time.time()) + repo.RUN_FOLLOWUP_CHECK_S - 5
    )
    assert await repo.end_run("owner_1", run_id, outcome="error", error_kind="late") is None
    assert not await repo.heartbeat_run("owner_1", run_id)
    assert not await repo.start_run("owner_1", "unknown", session_key="k")


@pytest.mark.asyncio
async def test_only_an_unacknowledged_run_is_redispatched(table):
    run_id, _ = await _dispatched()
    assert await repo.redispatch_run("owner_1", run_id)
    assert await repo.start_run("owner_1", run_id, session_key="k")
    assert await repo.redispatch_run("owner_1", run_id) is None  # started: never invoke it twice
    await repo.end_run("owner_1", run_id, outcome="completed")
    assert await repo.redispatch_run("owner_1", run_id) is None
    assert await repo.redispatch_run("owner_1", "unknown") is None


@pytest.mark.asyncio
async def test_sweeper_cannot_end_a_redispatch_with_a_new_deadline(table, monkeypatch):
    monkeypatch.setattr(repo.time, "time", lambda: 1_800_000_000)
    run_id, _ = await _dispatched()
    old_deadline = (await repo.get_run("owner_1", run_id))["deadlineAt"]
    monkeypatch.setattr(repo.time, "time", lambda: 1_800_000_100)
    renewed = await repo.redispatch_run("owner_1", run_id)
    assert renewed and renewed["deadlineAt"] > old_deadline

    assert await repo.end_run("owner_1", run_id, outcome="error", expected_deadline_at=old_deadline) is None
    assert (await repo.get_run("owner_1", run_id))["status"] == "dispatched"


@pytest.mark.asyncio
async def test_a_stop_pins_the_grace_deadline_until_the_run_ends(table):
    run_id, _ = await _dispatched()
    assert await repo.start_run("owner_1", run_id, session_key="k")
    assert await repo.request_abort("owner_1", run_id)
    pinned = (await repo.get_run("owner_1", run_id))["deadlineAt"]
    assert pinned <= int(time.time()) + repo.RUN_ABORT_GRACE_S
    assert not await repo.heartbeat_run("owner_1", run_id)  # a stopping run is not kept alive
    assert not await repo.request_abort("owner_1", run_id)  # a second Stop does not extend it
    assert (await repo.get_run("owner_1", run_id))["deadlineAt"] == pinned
    assert (await repo.end_run("owner_1", run_id, outcome="aborted", usage=USAGE))["outcome"] == "aborted"
    assert not await repo.request_abort("owner_1", run_id)


@pytest.mark.asyncio
async def test_overdue_query_sees_only_open_runs_past_their_deadline(table):
    run_id, _ = await _dispatched()
    now = int(time.time())
    assert await repo.list_overdue_runs(now) == []
    assert await repo.list_overdue_runs(now + repo.RUN_DISPATCH_TIMEOUT_S + 1) == [
        {"ownerId": "owner_1", "runId": run_id}
    ]
    await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert await repo.list_overdue_runs(now + 10_000) == []


@pytest.mark.asyncio
async def test_mac_viewer_demand_is_scoped_to_an_open_run(table):
    run_id, _ = await _dispatched()
    now = int(time.time())
    assert await repo.touch_mac_viewer("owner_1", run_id, "view", now)
    assert (await repo.get_run("owner_1", run_id))["macViewAt"] == now
    assert not await repo.touch_mac_viewer("owner_1", "unknown", "thumb", now)
    await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert not await repo.touch_mac_viewer("owner_1", run_id, "thumb", now)


@pytest.mark.asyncio
async def test_overdue_query_reads_past_a_full_first_page(table):
    now = int(time.time())
    with table.batch_writer() as batch:
        for index in range(120):
            batch.put_item(
                Item={
                    "pk": repo.owner_pk("owner_1"),
                    "sk": repo.run_sk(f"early-{index:03d}"),
                    "sweep": "open",
                    "deadlineAt": now - 2,
                }
            )
        batch.put_item(
            Item={
                "pk": repo.owner_pk("owner_2"),
                "sk": repo.run_sk("later"),
                "sweep": "open",
                "deadlineAt": now - 1,
            }
        )
        batch.put_item(
            Item={
                "pk": repo.owner_pk("owner_2"),
                "sk": repo.run_sk("future"),
                "sweep": "open",
                "deadlineAt": now + 1,
            }
        )
    overdue = await repo.list_overdue_runs(now)
    assert len(overdue) == 121
    assert {("owner_1", f"early-{index:03d}") for index in range(120)} <= {
        (ref["ownerId"], ref["runId"]) for ref in overdue
    }
    assert {"ownerId": "owner_2", "runId": "later"} in overdue
    assert {"ownerId": "owner_2", "runId": "future"} not in overdue


@pytest.mark.asyncio
async def test_open_runs_for_a_room_are_the_unended_ones_in_that_room(table):
    # What a client that opened the room mid-run needs: the run ledger, scoped
    # to one room, without the ended rows still sitting in the partition.
    mine, _ = await _dispatched(agent="a1", message="m1")
    other_room = repo.run_id_for("a2", "m2")
    await repo.create_run(
        "owner_1",
        other_room,
        room_id="33333333-3333-4333-8333-333333333333",
        agent_id="a2",
        workspace_id="ws",
        trigger_message_id="m2",
        event_kind="user_request",
    )
    ended, _ = await _dispatched(agent="a3", message="m3")
    await repo.end_run("owner_1", ended, outcome="completed", usage=USAGE)
    await repo.start_run("owner_1", mine, session_key="agent:a1:lightbulb:channel:x")

    open_runs = await repo.list_open_runs_for_room("owner_1", RUN_ROOM)
    assert [row["runId"] for row in open_runs] == [mine]
    assert open_runs[0]["agentId"] == "a1" and open_runs[0]["startedAt"]
    assert await repo.list_open_runs_for_room("owner_2", RUN_ROOM) == []


@pytest.mark.asyncio
async def test_has_open_runs_follows_the_sweep_marker(table):
    assert not await repo.has_open_runs("owner_1")
    run_id, _ = await _dispatched()
    assert await repo.has_open_runs("owner_1")
    assert not await repo.has_open_runs("owner_2")
    await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert not await repo.has_open_runs("owner_1")


VENDOR = "org_vendor"


def _foreign(user_id: str) -> dict:
    return {**_member("human", user_id), "homeOwnerId": VENDOR}


@pytest.mark.asyncio
async def test_foreign_member_is_indexed_by_home_owner(table):
    room_id, _ = await _room(members=[_member("human", USER_A, "owner"), _foreign("user_v")])
    row = await repo.get_member(room_id, "human", "user_v")
    assert row["memberKey"] == f"OWNER#{VENDOR}#human#user_v"
    assert row["ownerId"] == OWNER and repo.member_home(row, OWNER) == VENDOR
    assert [r["meta"]["roomId"] for r in await repo.list_rooms_for_member(VENDOR, "human", "user_v")] == [room_id]
    assert await repo.list_rooms_for_member(OWNER, "human", "user_v") == []
    local = await repo.get_member(room_id, "human", USER_A)
    assert local["memberKey"] == f"OWNER#{OWNER}#human#{USER_A}" and repo.member_home(local, OWNER) == OWNER


@pytest.mark.asyncio
async def test_upsert_keeps_the_members_home(table):
    room_id, _ = await _room()
    await repo.upsert_member(room_id=room_id, owner_id=OWNER, member=_foreign("user_v"))
    row = await repo.get_member(room_id, "human", "user_v")
    assert repo.member_home(row, OWNER) == VENDOR and row["memberKey"] == f"OWNER#{VENDOR}#human#user_v"


@pytest.mark.asyncio
async def test_removal_notice_carries_its_own_owner(table):
    room_id, meta = await _room(members=[_member("human", USER_A, "owner"), _foreign("user_v")])
    event = {"eventId": str(uuid.uuid4()), "type": "room.access.removed", "payload": {}}
    await repo.remove_member(
        meta=meta,
        kind="human",
        member_id="user_v",
        new_delivery_key=str(uuid.uuid4()),
        live_events=[{"userId": "user_v", "channelKey": "k", "event": event, "ownerId": VENDOR}],
    )
    live = await repo._query(repo.room_pk(room_id), begins_with="LIVE#")
    assert [(r["userId"], r["ownerId"]) for r in live] == [("user_v", VENDOR)]


def test_vendor_room_id_is_deterministic_per_customer():
    assert repo.vendor_room_id(OWNER) == repo.vendor_room_id(OWNER)
    assert repo.vendor_room_id(OWNER) != repo.vendor_room_id(VENDOR)
    assert repo.vendor_room_id(OWNER) == str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-vendor-room|{OWNER}"))


def test_member_item_carries_the_mode_marker_only_when_set():
    base = {"kind": "agent", "id": "a1", "role": "member", "listenMode": "mentions"}
    assert "modeSetBy" not in repo._member_item("r", "o", base, 1, 1, "t")
    assert repo._member_item("r", "o", {**base, "modeSetBy": "user"}, 1, 1, "t")["modeSetBy"] == "user"


@pytest.mark.asyncio
async def test_claim_autojoin_is_one_time_until_released(table):
    assert await repo.claim_autojoin("room-1", USER_A) is True
    assert await repo.claim_autojoin("room-1", USER_A) is False
    assert await repo.claim_autojoin("room-1", USER_B) is True
    await repo.release_autojoin("room-1", USER_A)
    assert await repo.claim_autojoin("room-1", USER_A) is True


@pytest.mark.asyncio
async def test_archived_room_refuses_every_post_writes_no_work_and_unarchive_restores_it(table):
    room_id, meta = await _room()
    archived = await repo.patch_room_meta(room_id, {"archivedAt": "2026-09-23T00:00:00+00:00", "archivedBy": USER_A})
    assert archived["archivedAt"] and archived["revision"] == meta["revision"] + 1
    with pytest.raises(repo.RoomArchived):
        await _post(room_id)
    rows = [i["sk"] for i in table.scan()["Items"] if i["pk"] == f"ROOM#{room_id}"]
    assert not any(sk.startswith(("WORK#", "MSG#")) for sk in rows)

    restored = await repo.patch_room_meta(room_id, {"archivedAt": None, "archivedBy": None})
    assert "archivedAt" not in restored and "archivedAt" not in await repo.get_room(room_id)
    assert (await _post(room_id))["messageSeq"] == 1


@pytest.mark.asyncio
async def test_archive_landing_between_the_meta_read_and_the_commit_still_refuses(table, monkeypatch):
    room_id, _ = await _room()
    stale = await repo.get_room(room_id)
    await repo.patch_room_meta(room_id, {"archivedAt": "2026-09-23T00:00:00+00:00", "archivedBy": USER_A})
    real_get_room = repo.get_room
    reads = iter([stale])
    monkeypatch.setattr(repo, "get_room", lambda rid: _first_then(reads, real_get_room, rid))
    with pytest.raises(repo.RoomArchived):
        await _post(room_id)
    assert (await real_get_room(room_id))["lastSeq"] == 0


async def _first_then(reads, fallback, rid):
    try:
        return next(reads)
    except StopIteration:
        return await fallback(rid)


@pytest.mark.asyncio
async def test_a_system_notice_needs_no_member_row_but_a_removed_human_is_still_refused(table):
    room_id, _ = await _room()
    notice = await _post(
        room_id, "Out of budget", [], "n-1", {"kind": "system", "id": "__system__", "displayName": "Lightbulb"}
    )
    assert notice["author"]["kind"] == "system" and notice["messageSeq"] == 1
    with pytest.raises(repo.MessageForbidden):
        await _post(room_id, "hi", [], "m-2", {"kind": "human", "id": "removed", "displayName": "Gone"})


@pytest.mark.asyncio
async def test_set_meeting_field_if_absent_has_one_winner(table):
    room_id, meta = await _room()
    assert not await repo.set_meeting_field_if_absent(room_id, "ticketNonce", "a")  # no meeting yet
    await repo.patch_room_meta(room_id, {"meeting": {"uid": "u1", "status": "scheduled"}})
    assert await repo.set_meeting_field_if_absent(room_id, "ticketNonce", "a", {"botClaimAt": 5})
    assert not await repo.set_meeting_field_if_absent(room_id, "ticketNonce", "b", {"botClaimAt": 6})
    stored = await repo.get_room(room_id)
    assert stored["meeting"] == {"uid": "u1", "status": "scheduled", "ticketNonce": "a", "botClaimAt": 5}
    assert stored["revision"] == meta["revision"] + 2


@pytest.mark.asyncio
async def test_patch_room_meta_bound_to_a_stale_revision_is_cancelled(table):
    from botocore.exceptions import ClientError

    room_id, meta = await _room()
    await repo.patch_room_meta(room_id, {"topic": "first"}, expected_revision=meta["revision"])
    with pytest.raises(ClientError) as lost:  # someone wrote since the caller's read
        await repo.patch_room_meta(room_id, {"topic": "stale"}, expected_revision=meta["revision"])
    assert repo._is_cancelled(lost.value)
    assert (await repo.get_room(room_id))["topic"] == "first"


async def test_a_meeting_notes_mail_is_claimed_once(table):
    assert not await repo.claim_meeting_notes_mail("owner_1", "msg-1")  # no request: nothing to mail
    await repo.put_meeting_notes_request("owner_1", "msg-1", {"roomId": "r1", "occurrence": 1790972400})
    assert (await repo.get_meeting_notes_request("owner_1", "msg-1"))["roomId"] == "r1"
    assert await repo.claim_meeting_notes_mail("owner_1", "msg-1")
    assert not await repo.claim_meeting_notes_mail("owner_1", "msg-1")
    await repo.put_meeting_notes_request("owner_1", "msg-1", {"roomId": "r1", "occurrence": 1790972400})  # a retry
    assert not await repo.claim_meeting_notes_mail("owner_1", "msg-1")


@pytest.mark.asyncio
async def test_an_ended_run_waits_for_its_followup_label_then_leaves_the_index(table):
    """T35: end_run parks the row as ``followup``; it is not open, and the label is written once."""
    run_id, _ = await _dispatched()
    await repo.end_run("owner_1", run_id, outcome="completed")
    assert not await repo.has_open_runs("owner_1")
    assert await repo.list_overdue_runs(int(time.time()) + 10) == []
    due = await repo.list_overdue_runs(int(time.time()) + repo.RUN_FOLLOWUP_CHECK_S + 10, marker="followup")
    assert due == [{"ownerId": "owner_1", "runId": run_id}]
    labelled = await repo.label_run_followup("owner_1", run_id, "replied")
    assert labelled["followUp"] == "replied" and "sweep" not in labelled and "deadlineAt" not in labelled
    assert await repo.label_run_followup("owner_1", run_id, "ignored") is None


@pytest.mark.asyncio
async def test_a_stop_records_who_and_when(table):
    run_id, _ = await _dispatched()
    assert await repo.start_run("owner_1", run_id, session_key="k")
    assert await repo.request_abort("owner_1", run_id, stopped_by="user_1", stop_after_s=12)
    row = await repo.get_run("owner_1", run_id)
    assert row["stoppedBy"] == "user_1" and row["stopAfterS"] == 12


@pytest.mark.asyncio
async def test_away_checks_ride_the_deadline_index_and_leave_it(table):
    """T15: due ``sweep = "away"`` rows are listed by key; defer moves the deadline; finish deletes."""
    now = int(time.time())
    for sk, deadline in (("M#r#due", now - 1), ("M#r#later", now + 600)):
        await repo.run_in_thread(
            repo._table().put_item, Item={"pk": "AWAY#user_b", "sk": sk, "sweep": "away", "deadlineAt": deadline}
        )
    assert await repo.list_due_away_checks(now) == [{"pk": "AWAY#user_b", "sk": "M#r#due"}]
    assert await repo.list_overdue_runs(now + 10_000) == []  # never mistaken for a run
    await repo.defer_away_check("AWAY#user_b", "M#r#due", now + 300)
    assert await repo.list_due_away_checks(now) == []
    await repo.finish_away_check("AWAY#user_b", "M#r#due")
    assert await repo.get_away_check("AWAY#user_b", "M#r#due") is None


def _switch_on(monkeypatch, org_name="Acme"):
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import messaging, task_conversation

    starter_ids = {key: f"10000000-0000-4000-8000-00000000000{i}" for i, key in enumerate(messaging._STARTER_KEYS, 1)}
    ceo = starter_ids["ceo"]

    async def owner_row(*_args, **_kwargs):
        return SimpleNamespace(status="active", starter_agent_ids=starter_ids)

    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", owner_row)
    roster = [{"id": agent_id, "name": key} for key, agent_id in starter_ids.items()]
    monkeypatch.setattr(messaging, "load_agent_roster", lambda _owner: asyncio.sleep(0, result=roster))
    monkeypatch.setattr(messaging, "human_display_name", lambda _user: asyncio.sleep(0, result="Workspace Owner"))
    monkeypatch.setattr(messaging, "_front_agent_id", lambda _owner: asyncio.sleep(0, result=ceo))
    monkeypatch.setattr(task_conversation, "front_agent_id", lambda _owner: asyncio.sleep(0, result=ceo))
    org = AsyncMock(return_value={"name": org_name})
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", org)
    return messaging, ceo, org


@pytest.mark.asyncio
async def test_with_the_switch_the_welcome_and_chips_go_to_the_owners_lightbulb_dm(table, monkeypatch):
    """UX slice 6: the welcome is the owner's Lightbulb DM's seq 1 (kind first_run), the DM is created with firstRun
    in the same write, and #general keeps one team line. A replay after an org rename changes nothing."""
    messaging, ceo, org = _switch_on(monkeypatch)
    created = []
    real_create_room = repo.create_room

    async def create_room(**kwargs):
        created.append(kwargs)
        return await real_create_room(**kwargs)

    monkeypatch.setattr(repo, "create_room", create_room)
    general = await messaging.ensure_default_room(OWNER, USER_A)
    dm = await messaging._agent_dm_room_id(OWNER, USER_A, ceo)
    assert next(c for c in created if c["kind"] == "dm")["extra"] == {"firstRun": True}  # no gap without the flag
    org.return_value = {"name": "Acme Renamed"}
    for _ in range(2):
        (welcome,), _ = await repo.list_messages(dm, 0, 100)
        assert (welcome["text"], welcome["kind"], welcome["author"]["displayName"]) == (
            messaging.default_room_welcome("Acme"),
            messaging.FIRST_RUN_KIND,
            messaging.FRONT_AGENT_NAME,
        )
        assert (await repo.get_room(dm))["firstRun"] is True
        (line,), _ = await repo.list_messages(general, 0, 100)
        assert (line["text"], line["kind"]) == (messaging.team_welcome("Acme"), messaging.TEAM_WELCOME_KIND)
        assert await messaging.ensure_default_room(OWNER, USER_A) == general
        # #general's welcome is the done signal; a second seed of the DM alone (a retry) replays under the new name.
        await messaging._post_first_run_welcome(OWNER, USER_A, ceo, messaging.default_room_welcome("Acme Renamed"))
    summaries = {s.room_id: s for s in await messaging._room_summaries(OWNER, USER_A)}
    assert summaries[uuid.UUID(dm)].first_run and not summaries[uuid.UUID(general)].first_run

    # The owner's first answer closes the first run; a later replay of the seed never reopens it.
    await messaging.close_first_run(dm)
    assert "firstRun" not in await repo.get_room(dm)
    await messaging._post_first_run_welcome(OWNER, USER_A, ceo, messaging.default_room_welcome("Acme"))
    assert "firstRun" not in await repo.get_room(dm)


@pytest.mark.asyncio
async def test_an_owner_who_wrote_before_setup_finished_still_gets_the_welcome_after_their_message(table, monkeypatch):
    from core.auth import AuthContext

    messaging, ceo, _ = _switch_on(monkeypatch)
    dm = await messaging._agent_dm_room_id(OWNER, USER_A, ceo)  # opened before provisioning seeded the welcome
    await messaging.create_human_message(
        AuthContext(user_id=USER_A, org_id=OWNER),
        dm,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(), text="hello?", reply_to_message_id=None, thread_root_message_id=None
        ),
    )
    await messaging.ensure_default_room(OWNER, USER_A)
    rows, _ = await repo.list_messages(dm, 0, 100)
    assert [(r["author"]["kind"], r.get("kind")) for r in rows] == [
        ("human", None),
        ("agent", messaging.FIRST_RUN_KIND),
    ]
    assert (await repo.get_room(dm))["firstRun"] is True


@pytest.mark.asyncio
async def test_paused_ask_keeps_the_latest_and_pops_once(table):
    """W18-8: each person's latest ask while paused (an older redelivery never replaces it); Resume takes the
    resumer's own, else the latest anyone's, and clears the window."""
    room = str(uuid.uuid4())
    assert await repo.pop_paused_ask(room) is None
    await repo.put_paused_ask(room, 5, USER_A)
    await repo.put_paused_ask(room, 3, USER_A)
    await repo.put_paused_ask(room, 7, USER_B)
    assert await repo.pop_paused_ask(room, USER_A) == {"seq": 5, "authorId": USER_A}
    assert await repo.pop_paused_ask(room, USER_A) is None
    await repo.put_paused_ask(room, 5, USER_A)
    await repo.put_paused_ask(room, 7, USER_B)
    assert await repo.pop_paused_ask(room, "someone-else") == {"seq": 7, "authorId": USER_B}
