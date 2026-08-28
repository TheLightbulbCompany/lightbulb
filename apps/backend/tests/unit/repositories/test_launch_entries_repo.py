import os
from unittest.mock import patch

import boto3
import pytest
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


async def test_upsert_creates_then_preserves_entered_at_and_revenue(table):
    row = await table.upsert(
        "user_1", display_name="Ada", public=True, waitlist_position=7, currency="usd", baseline_cents=1000
    )
    assert row["entered_at"] and row["delta_cents"] == 0 and row["window_cents"] == 0
    await table.set_revenue("user_1", window_cents=2500, delta_cents=1500, currency="usd")
    again = await table.upsert(
        "user_1", display_name="Ada L.", public=False, waitlist_position=7, currency="usd", baseline_cents=1200
    )
    assert again["entered_at"] == row["entered_at"]
    assert again["display_name"] == "Ada L." and again["public"] is False
    assert again["baseline_cents"] == 1200 and again["window_cents"] == 2500


async def test_set_revenue_clears_error_and_stamps(table):
    await table.upsert(
        "user_1", display_name="Ada", public=True, waitlist_position=None, currency="usd", baseline_cents=0
    )
    await table.set_refresh_error("user_1", "stripe_auth")
    row = await table.get("user_1")
    assert row["refresh_error"] == "stripe_auth" and row["refreshed_at"]
    await table.set_revenue("user_1", window_cents=10, delta_cents=10, currency="usd")
    row = await table.get("user_1")
    assert "refresh_error" not in row and row["delta_cents"] == 10


async def test_list_count_delete(table):
    await table.upsert(
        "user_1", display_name="A", public=True, waitlist_position=None, currency="usd", baseline_cents=0
    )
    await table.upsert("org_2", display_name="B", public=False, waitlist_position=3, currency="eur", baseline_cents=5)
    assert await table.count() == 2
    assert {r["owner_id"] for r in await table.list_all()} == {"user_1", "org_2"}
    await table.delete("user_1")
    assert await table.get("user_1") is None
