import os
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-entries"


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_entries_repo as repo

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield repo


async def test_join_creates_an_unkeyed_row(table):
    row = await table.join("user_1", display_name="Ada", public=True, waitlist_position=7)
    assert row["entered_at"]
    assert row["has_key"] is False
    assert row["baseline_cents"] == 0 and row["window_cents"] == 0 and row["delta_cents"] == 0
    assert row["display_name"] == "Ada" and row["public"] is True and row["waitlist_position"] == 7


async def test_second_join_returns_the_existing_row_untouched(table):
    first = await table.join("user_1", display_name="Ada", public=True, waitlist_position=7)
    again = await table.join("user_1", display_name="Ada L.", public=False, waitlist_position=99)
    assert again == first  # first write wins — display_name/public/waitlist_position did NOT change


async def test_join_retries_the_put_once_when_the_winner_row_vanishes_then_persists(table):
    """The put lost the race, then the winning row was deleted before the
    read. A bare `_plain(item)` fallback would hand back an item DynamoDB
    never holds — instead the conditional put is retried once, and this
    time it lands for real."""
    from unittest.mock import AsyncMock

    real_table = table._table()
    real_put_item = real_table.put_item
    calls = {"n": 0}

    def flaky_put_item(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "boom"}}, "PutItem")
        return real_put_item(**kwargs)

    with (
        patch.object(table, "_table", return_value=real_table),
        patch.object(table, "get", AsyncMock(return_value=None)),
        patch.object(real_table, "put_item", side_effect=flaky_put_item),
    ):
        row = await table.join("user_1", display_name="Ada", public=True, waitlist_position=7)

    assert calls["n"] == 2  # the retry actually happened
    assert row is not None and row["owner_id"] == "user_1" and row["has_key"] is False
    # Bypass the patched `get` — prove the retried put actually persisted,
    # not just that `join` returned a locally-built dict.
    persisted = real_table.get_item(Key={"owner_id": "user_1"})["Item"]
    assert persisted["owner_id"] == "user_1"


async def test_join_conflict_reads_use_consistent_read(table):
    """Both conflict-resolution reads inside join() must pass ConsistentRead=True:
    an eventually-consistent GetItem right after a ConditionalCheckFailedException
    can miss the winning row and send the retried put down the doomsday path (or
    a spurious 500), even though the row is really there."""
    await table.join("user_1", display_name="Ada", public=True, waitlist_position=7)

    real_table = table._table()
    real_get_item = real_table.get_item
    calls = []

    def spy_get_item(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {}  # simulate the winner row looking vanished on the first (stale) read
        return real_get_item(**kwargs)

    def always_conflict_put(**kwargs):
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "boom"}}, "PutItem")

    with (
        patch.object(table, "_table", return_value=real_table),
        patch.object(real_table, "get_item", side_effect=spy_get_item),
        patch.object(real_table, "put_item", side_effect=always_conflict_put),
    ):
        row = await table.join("user_1", display_name="Ada L.", public=False, waitlist_position=99)

    assert len(calls) == 2  # both conflict-resolution reads fired
    assert all(c.get("ConsistentRead") is True for c in calls)
    assert row["owner_id"] == "user_1"


async def test_set_key_connected_flips_has_key_and_stamps_baseline(table):
    await table.join("user_1", display_name="Ada", public=True, waitlist_position=None)
    await table.set_key_connected("user_1", currency="usd", baseline_cents=1000, baseline_at="2026-10-01T00:00:00Z")
    row = await table.get("user_1")
    assert row["has_key"] is True
    assert row["baseline_cents"] == 1000
    assert row["currency"] == "usd"
    assert row["baseline_at"] == "2026-10-01T00:00:00Z"
    # A row that does not exist is not created (the caller routes this through _record).
    with pytest.raises(ClientError):
        await table.set_key_connected("ghost", currency="usd", baseline_cents=1, baseline_at="t")
    assert await table.get("ghost") is None


async def test_set_revenue_clears_error_and_stamps(table):
    await table.join("user_1", display_name="Ada", public=True, waitlist_position=None)
    await table.set_refresh_error("user_1", "stripe_auth")
    row = await table.get("user_1")
    assert row["refresh_error"] == "stripe_auth" and row["refreshed_at"]
    await table.set_revenue("user_1", window_cents=10, delta_cents=10, currency="usd")
    row = await table.get("user_1")
    assert "refresh_error" not in row and row["delta_cents"] == 10


async def test_set_public_flips_the_flag(table):
    await table.join("user_1", display_name="Ada", public=True, waitlist_position=None)
    await table.set_public("user_1", False)
    assert (await table.get("user_1"))["public"] is False
    await table.set_public("user_1", True)
    assert (await table.get("user_1"))["public"] is True
    # A row that does not exist is not created.
    with pytest.raises(ClientError):
        await table.set_public("ghost", True)
    assert await table.get("ghost") is None


async def test_list_count_delete(table):
    await table.join("user_1", display_name="A", public=True, waitlist_position=None)
    await table.join("org_2", display_name="B", public=False, waitlist_position=3)
    await table.set_key_connected("org_2", currency="eur", baseline_cents=5, baseline_at="t")
    assert await table.count() == 2
    assert {r["owner_id"] for r in await table.list_all()} == {"user_1", "org_2"}
    await table.delete("user_1")
    assert await table.get("user_1") is None
