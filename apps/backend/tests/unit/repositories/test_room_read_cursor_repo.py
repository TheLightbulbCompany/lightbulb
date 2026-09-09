"""Focused persistence contract for per-human native-room read cursors."""

import asyncio
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def cursor_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-room-read-cursors",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "cursor_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "cursor_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=table.name)
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_cursor_advances_monotonically_per_owner_profile_and_room(cursor_table):
    from core.repositories import room_read_cursor_repo as repo

    first = await repo.advance("org_a", "profile_a", "agent:friday:room:a", 8)
    stale = await repo.advance("org_a", "profile_a", "agent:friday:room:a", 3)
    newer = await repo.advance("org_a", "profile_a", "agent:friday:room:a", 11)

    assert first["last_read_message_seq"] == 8
    assert first["advanced"] is True
    assert stale["last_read_message_seq"] == 8
    assert stale["advanced"] is False
    assert newer["last_read_message_seq"] == 11
    assert newer["advanced"] is True

    assert await repo.get("org_a", "profile_b", "agent:friday:room:a") is None
    assert await repo.get("org_b", "profile_a", "agent:friday:room:a") is None
    assert await repo.get("org_a", "profile_a", "agent:friday:room:b") is None


@pytest.mark.asyncio
async def test_cursor_row_contains_no_transcript_or_identity_presentation_data(cursor_table):
    from core.repositories import room_read_cursor_repo as repo

    await repo.advance("org_a", "profile_a", "agent:friday:room:a", 4)
    rows = cursor_table.scan()["Items"]

    assert len(rows) == 1
    assert set(rows[0]) == {
        "owner_id",
        "cursor_key",
        "profile_id",
        "room_key",
        "last_read_message_seq",
        "updated_at",
    }
    assert "message" not in rows[0]
    assert "body" not in rows[0]


@pytest.mark.asyncio
async def test_concurrent_cursor_writers_converge_on_highest_sequence(cursor_table):
    from core.repositories import room_read_cursor_repo as repo

    await asyncio.gather(*(repo.advance("org_a", "profile_a", "agent:friday:room:a", seq) for seq in range(1, 17)))

    stored = await repo.get("org_a", "profile_a", "agent:friday:room:a")
    assert stored is not None
    assert stored["last_read_message_seq"] == 16


@pytest.mark.asyncio
async def test_account_wipe_deletes_only_the_owner_cursor_partition(cursor_table):
    from core.repositories import room_read_cursor_repo as repo

    await repo.advance("org_a", "profile_a", "agent:friday:room:a", 4)
    await repo.advance("org_a", "profile_b", "agent:friday:room:b", 7)
    await repo.advance("org_b", "profile_a", "agent:friday:room:a", 9)

    assert await repo.delete_all_for_owner("org_a") == 2
    remaining = cursor_table.scan()["Items"]
    assert len(remaining) == 1
    assert remaining[0]["owner_id"] == "org_b"
