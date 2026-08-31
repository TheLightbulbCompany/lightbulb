"""Tests for the rollup day-row DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

VALUES = {
    "revenue_microcents": 1_230_000,
    "visitors": 42,
    "signups": 3,
    "followers": 0,
    "open_threads": 5,
    "agent_spend_microcents": 987_654,
}


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-usage-counters",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "period", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "period", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield


@pytest.mark.asyncio
async def test_put_and_get_round_trips_all_six_values(dynamodb_table):
    from core.repositories import rollup_repo

    await rollup_repo.put_day("owner_1", "2026-08-30", VALUES)
    rows = await rollup_repo.get_days("owner_1", "2026-08-01", "2026-08-31")
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-08-30"
    for key, expected in VALUES.items():
        assert row[key] == expected
        assert isinstance(row[key], int)


@pytest.mark.asyncio
async def test_get_days_range_and_namespace(dynamodb_table):
    from core.repositories import rollup_repo, usage_repo

    await rollup_repo.put_day("owner_1", "2026-08-15", VALUES)
    await rollup_repo.put_day("owner_1", "2026-08-20", VALUES)
    await rollup_repo.put_day("owner_1", "2026-09-01", VALUES)  # outside window
    # A month rollup row on the same table must not leak into day results.
    await usage_repo.increment("owner_1", "2026-08", 100, 1, 1, 0, 0)

    rows = await rollup_repo.get_days("owner_1", "2026-08-01", "2026-08-31")
    assert [r["day"] for r in rows] == ["2026-08-15", "2026-08-20"]


@pytest.mark.asyncio
async def test_put_day_overwrites_not_adds(dynamodb_table):
    from core.repositories import rollup_repo

    await rollup_repo.put_day("owner_1", "2026-08-30", VALUES)
    await rollup_repo.put_day("owner_1", "2026-08-30", {**VALUES, "visitors": 7})
    rows = await rollup_repo.get_days("owner_1", "2026-08-30", "2026-08-30")
    assert len(rows) == 1
    assert rows[0]["visitors"] == 7
    assert rows[0]["revenue_microcents"] == VALUES["revenue_microcents"]


@pytest.mark.asyncio
async def test_delete_all_for_owner_only_day_rows(dynamodb_table):
    from core.repositories import rollup_repo, usage_repo

    await rollup_repo.put_day("owner_1", "2026-08-29", VALUES)
    await rollup_repo.put_day("owner_1", "2026-08-30", VALUES)
    await usage_repo.increment("owner_1", "2026-08", 100, 1, 1, 0, 0)
    await usage_repo.increment("owner_1", "lifetime", 100, 1, 1, 0, 0)

    deleted = await rollup_repo.delete_all_for_owner("owner_1")
    assert deleted == 2
    assert await rollup_repo.get_days("owner_1", "2026-01-01", "2026-12-31") == []
    # Non-day rows survive.
    assert await usage_repo.get_period_usage("owner_1", "2026-08") is not None
    assert await usage_repo.get_period_usage("owner_1", "lifetime") is not None


@pytest.mark.asyncio
async def test_get_days_empty(dynamodb_table):
    from core.repositories import rollup_repo

    assert await rollup_repo.get_days("owner_1", "2026-08-01", "2026-08-31") == []
