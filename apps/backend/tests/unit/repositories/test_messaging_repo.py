"""Canonical messaging table — transaction shapes and ordering guarantees."""

import time
from unittest.mock import patch

import boto3
import pytest
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
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-member",
                    "KeySchema": [
                        {"AttributeName": "memberKey", "KeyType": "HASH"},
                        {"AttributeName": "memberSort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
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


async def _room(members=None, client_room_id="c-room-1", name="launch"):
    members = members or [_member("human", USER_A, "owner"), _member("agent", FRIDAY, handle="friday")]
    room_id = repo.deterministic_room_id(OWNER, USER_A, client_room_id)
    meta = await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=client_room_id,
        creator_user_id=USER_A,
        name=name,
        kind="channel",
        members=members,
        req_hash=repo.request_hash(name, "channel", members),
    )
    return room_id, meta


async def _post(room_id, text="hi", targets=None, client_id="m-1", author=None):
    author = author or {"kind": "human", "id": USER_A, "displayName": "Ann"}
    return await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.room_pk(room_id),
        idemp_sk=repo.human_idemp_sk(USER_A, client_id),
        req_hash=repo.request_hash(text),
        author=author,
        text=text,
        mentions=[],
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=None,
        dispatch_targets=targets if targets is not None else [{"agentId": FRIDAY, "eventKind": "room_event"}],
        source_agent_id=None,
        client_message_id=client_id,
    )


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
    first = await _post(room_id, text="same")
    again = await _post(room_id, text="same")
    assert again["messageId"] == first["messageId"] and (await repo.get_room(room_id))["lastSeq"] == 1
    with pytest.raises(repo.IdempotencyConflict):
        await _post(room_id, text="different")


@pytest.mark.asyncio
async def test_agent_receipt_is_owner_scoped_by_event_id(table):
    room_id, _ = await _room()
    author = {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}
    m = await repo.create_message(
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
async def test_sequence_conflict_is_retried(table):
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
async def test_gsi_is_discovery_only_and_base_membership_is_strong(table):
    room_id, _ = await _room()
    found = await repo.list_rooms_for_member(OWNER, "human", USER_A)
    assert [f["meta"]["roomId"] for f in found] == [room_id]
    assert await repo.list_rooms_for_member("org_other", "human", USER_A) == []
    # A stale GSI entry can never pass the base read.
    await repo.remove_member(room_id=room_id, owner_id=OWNER, kind="human", member_id=USER_A)
    assert await repo.get_member(room_id, "human", USER_A) is None


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
        room_id=room_id, owner_id=OWNER, kind="human", member_id=USER_B, live_events=live
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
        await repo.remove_member(room_id=room_id, owner_id=OWNER, kind="human", member_id=USER_B)
    assert (await repo.get_room(room_id))["deliveryKey"] == new_meta["deliveryKey"]


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
    await repo.remove_member(room_id=room_id, owner_id=OWNER, kind="agent", member_id=GAUGE)
    assert await repo.get_agent_delivery(room_id, GAUGE) is None
    assert (await repo.get_room(room_id))["deliveryKey"] == before


@pytest.mark.asyncio
async def test_agent_cursor_is_compare_and_set(table):
    room_id, _ = await _room()
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=0, through=3) is True
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=0, through=5) is False  # moved
    assert await repo.advance_agent_delivery(room_id, FRIDAY, expected=3, through=2) is False  # backward
    assert await repo.advance_agent_delivery(room_id, GAUGE, expected=0, through=1) is False  # absent
    assert (await repo.get_agent_delivery(room_id, FRIDAY))["lastSeq"] == 3


@pytest.mark.asyncio
async def test_read_cursor_is_monotonic_and_bounded(table):
    room_id, _ = await _room()
    await _post(room_id, client_id="m-1")
    await _post(room_id, client_id="m-2")
    assert await repo.set_read_cursor(room_id, USER_A, 9) == 2
    assert await repo.set_read_cursor(room_id, USER_A, 1) == 2
    assert await repo.get_read_cursor(room_id, USER_B) == 0


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
    second = await repo.ensure_user_stream(OWNER, USER_A)
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
    assert await repo.list_owner_room_ids(OWNER) == [room_id]
    assert await repo.delete_room_partition(room_id) > 0
    assert await repo.delete_owner_partition(OWNER) > 0
    assert await repo.get_room(room_id) is None and await repo.get_stream(key) is None
    assert await repo.get_stream(other_key) is not None
    assert await repo.delete_room_partition(room_id) == 0  # idempotent retry
