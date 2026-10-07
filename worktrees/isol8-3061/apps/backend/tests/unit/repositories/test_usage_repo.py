"""Tests for usage counter DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


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
async def test_increment_creates_item(dynamodb_table):
    from core.repositories import usage_repo

    await usage_repo.increment("owner_1", "2026-03", 100_000, 500, 200, 50, 10)
    result = await usage_repo.get_period_usage("owner_1", "2026-03")
    assert result["total_spend_microdollars"] == 100_000
    assert result["total_input_tokens"] == 500
    assert result["request_count"] == 1


@pytest.mark.asyncio
async def test_increment_adds_atomically(dynamodb_table):
    from core.repositories import usage_repo

    await usage_repo.increment("owner_1", "2026-03", 100_000, 500, 200, 0, 0)
    await usage_repo.increment("owner_1", "2026-03", 50_000, 300, 100, 0, 0)
    result = await usage_repo.get_period_usage("owner_1", "2026-03")
    assert result["total_spend_microdollars"] == 150_000
    assert result["total_input_tokens"] == 800
    assert result["request_count"] == 2


@pytest.mark.asyncio
async def test_get_nonexistent_returns_none(dynamodb_table):
    from core.repositories import usage_repo

    assert await usage_repo.get_period_usage("owner_1", "2026-03") is None


@pytest.mark.asyncio
async def test_lifetime_and_monthly_independent(dynamodb_table):
    from core.repositories import usage_repo

    await usage_repo.increment("owner_1", "2026-03", 100_000, 500, 200, 0, 0)
    await usage_repo.increment("owner_1", "lifetime", 100_000, 500, 200, 0, 0)
    march = await usage_repo.get_period_usage("owner_1", "2026-03")
    lifetime = await usage_repo.get_period_usage("owner_1", "lifetime")
    assert march["total_spend_microdollars"] == 100_000
    assert lifetime["total_spend_microdollars"] == 100_000


@pytest.mark.asyncio
async def test_member_tracking(dynamodb_table):
    from core.repositories import usage_repo

    await usage_repo.increment("org_1", "member:user_a:2026-03", 80_000, 400, 100, 0, 0)
    await usage_repo.increment("org_1", "member:user_b:2026-03", 20_000, 100, 50, 0, 0)
    members = await usage_repo.get_member_usage("org_1", "2026-03")
    assert len(members) == 2
    user_a = next(m for m in members if m["user_id"] == "user_a")
    assert user_a["total_spend_microdollars"] == 80_000


@pytest.mark.asyncio
async def test_get_member_usage_empty(dynamodb_table):
    from core.repositories import usage_repo

    members = await usage_repo.get_member_usage("org_1", "2026-03")
    assert members == []
