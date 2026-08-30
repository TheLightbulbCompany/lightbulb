import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-settings"


@pytest.fixture
def repo():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_settings_repo as repo

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield repo


async def test_get_returns_none_when_no_row_saved_yet(repo):
    assert await repo.get() is None


def test_get_sync_returns_none_when_no_row_saved_yet(repo):
    assert repo.get_sync() is None


async def test_put_then_get_round_trips(repo):
    row = dict(repo.DEFAULTS, enabled=True, drop_at="2026-09-18T00:00:00-04:00", updated_by="user_admin")
    saved = await repo.put(row)
    assert saved["enabled"] is True and saved["drop_at"] == "2026-09-18T00:00:00-04:00"
    assert saved["updated_by"] == "user_admin"
    assert "id" not in saved  # the partition key is an implementation detail, not part of the row

    fetched = await repo.get()
    assert fetched == saved


def test_put_then_get_sync_round_trips(repo):
    import asyncio

    row = dict(repo.DEFAULTS, comp_slots=500)
    asyncio.run(repo.put(row))
    fetched = repo.get_sync()
    assert fetched["comp_slots"] == 500
    assert "id" not in fetched


async def test_put_stamps_updated_at(repo):
    row = dict(repo.DEFAULTS)
    assert "updated_at" not in row
    saved = await repo.put(row)
    assert saved["updated_at"]


async def test_put_is_a_full_replace(repo):
    """A second put with a smaller payload must not leave fields from the
    first put behind — put() replaces the whole item, it doesn't merge."""
    first = dict(repo.DEFAULTS, stripe_coupon_id="promo_old", comp_slots=300)
    await repo.put(first)

    second = dict(repo.DEFAULTS)  # stripe_coupon_id defaults back to ""
    await repo.put(second)

    fetched = await repo.get()
    assert fetched["stripe_coupon_id"] == "" and fetched["comp_slots"] == 220


async def test_put_always_writes_to_the_singleton_id(repo):
    await repo.put(dict(repo.DEFAULTS, id="something-else"))
    # A caller-supplied id must not fork the row — put() always writes id="settings".
    resp = repo._table().scan()
    assert len(resp["Items"]) == 1 and resp["Items"][0]["id"] == "settings"
