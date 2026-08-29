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


async def test_list_count_count_with_key_delete(table):
    await table.join("user_1", display_name="A", public=True, waitlist_position=None)
    await table.join("org_2", display_name="B", public=False, waitlist_position=3)
    await table.set_key_connected("org_2", currency="eur", baseline_cents=5, baseline_at="t")
    assert await table.count() == 2
    assert await table.count_with_key() == 1
    assert {r["owner_id"] for r in await table.list_all()} == {"user_1", "org_2"}
    await table.delete("user_1")
    assert await table.get("user_1") is None
