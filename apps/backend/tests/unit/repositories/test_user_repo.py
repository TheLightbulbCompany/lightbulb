"""Tests for user DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

# Patch settings before importing repo modules
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB users table."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-users",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-users")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_put_and_get(dynamodb_table):
    from core.repositories import user_repo

    result = await user_repo.put("user_abc")
    assert result["user_id"] == "user_abc"
    assert "created_at" in result

    item = await user_repo.get("user_abc")
    assert item is not None
    assert item["user_id"] == "user_abc"


@pytest.mark.asyncio
async def test_put_persists_email_when_supplied(dynamodb_table):
    """The Clerk user.created webhook supplies the primary email on put;
    verifying it round-trips through DynamoDB so owner-email lookups can read it back."""
    from core.repositories import user_repo

    result = await user_repo.put("user_email", email="owner@example.test")
    assert result["email"] == "owner@example.test"

    item = await user_repo.get("user_email")
    assert item is not None
    assert item["email"] == "owner@example.test"


@pytest.mark.asyncio
async def test_put_omits_email_when_none(dynamodb_table):
    """Calls without an email (e.g. /users/sync where only the user_id is
    known) must NOT write an empty string — that would shadow Clerk's
    primary email on a later upsert."""
    from core.repositories import user_repo

    await user_repo.put("user_no_email")
    item = await user_repo.get("user_no_email")
    assert item is not None
    assert "email" not in item


@pytest.mark.asyncio
async def test_get_nonexistent(dynamodb_table):
    from core.repositories import user_repo

    item = await user_repo.get("does_not_exist")
    assert item is None


@pytest.mark.asyncio
async def test_delete(dynamodb_table):
    from core.repositories import user_repo

    await user_repo.put("user_del")
    await user_repo.delete("user_del")
    item = await user_repo.get("user_del")
    assert item is None


@pytest.mark.asyncio
async def test_delete_nonexistent_no_error(dynamodb_table):
    from core.repositories import user_repo

    # Should not raise
    await user_repo.delete("ghost_user")
