"""Tests for billing account DynamoDB repository."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB billing-accounts table with Stripe GSI."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-billing-accounts")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_create_and_get(dynamodb_table):
    from core.repositories import billing_repo

    result = await billing_repo.create_if_not_exists("user_1", "cus_abc")
    assert result["owner_id"] == "user_1"
    assert result["stripe_customer_id"] == "cus_abc"
    assert "id" in result

    item = await billing_repo.get_by_owner_id("user_1")
    assert item is not None
    assert item["stripe_customer_id"] == "cus_abc"


@pytest.mark.asyncio
async def test_get_by_owner_id_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    item = await billing_repo.get_by_owner_id("ghost")
    assert item is None


@pytest.mark.asyncio
async def test_get_by_stripe_customer_id(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_2", "cus_def")
    item = await billing_repo.get_by_stripe_customer_id("cus_def")
    assert item is not None
    assert item["owner_id"] == "user_2"


@pytest.mark.asyncio
async def test_get_by_stripe_customer_id_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    item = await billing_repo.get_by_stripe_customer_id("cus_nope")
    assert item is None


@pytest.mark.asyncio
async def test_create_if_not_exists_rejects_duplicate(dynamodb_table):
    from core.repositories import billing_repo
    from core.repositories.billing_repo import AlreadyExistsError

    await billing_repo.create_if_not_exists("user_3", "cus_ghi")
    with pytest.raises(AlreadyExistsError):
        await billing_repo.create_if_not_exists("user_3", "cus_ghi_dup")


@pytest.mark.asyncio
async def test_set_subscription_persists_status_and_trial_end(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_4", "cus_jkl")
    result = await billing_repo.set_subscription(
        owner_id="user_4",
        subscription_id="sub_xyz",
        status="trialing",
        trial_end=1700000000,
    )
    assert result is not None
    assert result["stripe_subscription_id"] == "sub_xyz"
    assert result["subscription_status"] == "trialing"
    assert result["trial_end"] == 1700000000


@pytest.mark.asyncio
async def test_set_subscription_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    result = await billing_repo.set_subscription(owner_id="ghost", subscription_id="sub_x", status="active")
    assert result is None


@pytest.mark.asyncio
async def test_delete(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_5", "cus_mno")
    await billing_repo.delete("user_5")
    item = await billing_repo.get_by_owner_id("user_5")
    assert item is None


@pytest.mark.asyncio
async def test_set_and_get_by_metronome_customer_id(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_m", "cus_m")
    await billing_repo.set_metronome_customer_id("org_m", "cust-uuid-1")

    item = await billing_repo.get_by_owner_id("org_m")
    assert item["metronome_customer_id"] == "cust-uuid-1"

    # The webhook resolves owner via the Metronome UUID.
    resolved = await billing_repo.get_by_metronome_customer_id("cust-uuid-1")
    assert resolved is not None
    assert resolved["owner_id"] == "org_m"

    assert await billing_repo.get_by_metronome_customer_id("unknown-uuid") is None


@pytest.mark.asyncio
async def test_set_metronome_customer_id_requires_existing_row(dynamodb_table):
    """The conditional write must not upsert a phantom billing row (same trap
    documented on clear_provider_choice)."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.set_metronome_customer_id("org_ghost", "cust-uuid-1")
    assert await billing_repo.get_by_owner_id("org_ghost") is None
