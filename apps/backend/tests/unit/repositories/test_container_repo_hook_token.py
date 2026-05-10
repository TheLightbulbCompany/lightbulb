"""Tests for ContainerRepo hook_token storage."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB containers table with GSIs."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "gateway_token", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gateway-token-index",
                    "KeySchema": [{"AttributeName": "gateway_token", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-index",
                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_set_hook_token_persists_token_and_timestamp(dynamodb_table):
    """set_hook_token writes hook_token + hook_token_rotated_at onto the row."""
    from core.repositories import container_repo

    await container_repo.upsert(
        "u_1",
        {"gateway_token": "tok_1", "status": "running"},
    )

    result = await container_repo.set_hook_token("u_1", "abc-token")

    assert result is not None
    assert result["hook_token"] == "abc-token"

    row = await container_repo.get_by_owner_id("u_1")
    assert row is not None
    assert row["hook_token"] == "abc-token"
    # Timestamp is set to a non-empty ISO string; we don't pin the exact value
    # because behavior is "rotation timestamp tracks the current token", not
    # "calls a particular clock function".
    assert isinstance(row["hook_token_rotated_at"], str)
    assert row["hook_token_rotated_at"]


@pytest.mark.asyncio
async def test_set_hook_token_returns_none_when_owner_missing(dynamodb_table):
    """If the row doesn't exist, update_fields returns None and we propagate it."""
    from core.repositories import container_repo

    result = await container_repo.set_hook_token("u_unknown", "abc-token")

    assert result is None
    # And nothing was written.
    row = await container_repo.get_by_owner_id("u_unknown")
    assert row is None


@pytest.mark.asyncio
async def test_set_hook_token_uses_module_level_utc_now_iso(dynamodb_table):
    """Documentation test: rotation timestamp comes from the module-level
    ``utc_now_iso`` import, so callers wanting a deterministic timestamp can
    patch ``core.repositories.container_repo.utc_now_iso``.
    """
    from core.repositories import container_repo

    await container_repo.upsert(
        "u_2",
        {"gateway_token": "tok_2", "status": "running"},
    )

    fake_now = "2026-05-09T12:00:00+00:00"
    with patch.object(container_repo, "utc_now_iso", return_value=fake_now):
        await container_repo.set_hook_token("u_2", "tok-xyz")

    row = await container_repo.get_by_owner_id("u_2")
    assert row["hook_token"] == "tok-xyz"
    assert row["hook_token_rotated_at"] == fake_now
