"""Tests for billing_repo.set_provider_choice with org invariant."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.repositories import billing_repo


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
async def test_set_provider_choice_personal_bedrock(dynamodb_table):
    await billing_repo.create_if_not_exists("user_x", "cus_abc", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_x",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="personal",
    )
    row = await billing_repo.get_by_owner_id("user_x")
    assert row["provider_choice"] == "bedrock_claude"
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_set_provider_choice_personal_byo_key(dynamodb_table):
    await billing_repo.create_if_not_exists("user_y", "cus_def", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_y",
        provider_choice="byo_key",
        byo_provider="openai",
        owner_type="personal",
    )
    row = await billing_repo.get_by_owner_id("user_y")
    assert row["provider_choice"] == "byo_key"
    assert row["byo_provider"] == "openai"


@pytest.mark.asyncio
async def test_set_provider_choice_personal_chatgpt_oauth(dynamodb_table):
    await billing_repo.create_if_not_exists("user_z", "cus_ghi", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_z",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        owner_type="personal",
    )
    row = await billing_repo.get_by_owner_id("user_z")
    assert row["provider_choice"] == "chatgpt_oauth"


@pytest.mark.asyncio
async def test_set_provider_choice_org_chatgpt_oauth_rejected(dynamodb_table):
    """ChatGPT OAuth cannot be set on org owners (decision 2026-04-30)."""
    await billing_repo.create_if_not_exists("org_x", "cus_jkl", owner_type="org")
    with pytest.raises(ValueError, match="chatgpt_oauth"):
        await billing_repo.set_provider_choice(
            "org_x",
            provider_choice="chatgpt_oauth",
            byo_provider=None,
            owner_type="org",
        )


@pytest.mark.asyncio
async def test_set_provider_choice_org_bedrock(dynamodb_table):
    await billing_repo.create_if_not_exists("org_y", "cus_mno", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_y",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="org",
    )
    row = await billing_repo.get_by_owner_id("org_y")
    assert row["provider_choice"] == "bedrock_claude"


@pytest.mark.asyncio
async def test_set_provider_choice_org_byo_key(dynamodb_table):
    await billing_repo.create_if_not_exists("org_z", "cus_pqr", owner_type="org")
    await billing_repo.set_provider_choice(
        "org_z",
        provider_choice="byo_key",
        byo_provider="anthropic",
        owner_type="org",
    )
    row = await billing_repo.get_by_owner_id("org_z")
    assert row["provider_choice"] == "byo_key"
    assert row["byo_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_set_provider_choice_unknown_provider_rejected(dynamodb_table):
    await billing_repo.create_if_not_exists("user_w", "cus_stu", owner_type="personal")
    with pytest.raises(ValueError, match="unknown provider_choice"):
        await billing_repo.set_provider_choice(
            "user_w",
            provider_choice="invalid_choice",
            byo_provider=None,
            owner_type="personal",
        )


@pytest.mark.asyncio
async def test_set_provider_choice_byo_key_without_provider_now_allowed(dynamodb_table):
    """byo_provider can be set later via the BYO settings step; not required
    at /trial-checkout time. The picker submits {provider_choice: 'byo_key'}
    alone.
    """
    await billing_repo.create_if_not_exists("user_v", "cus_vwx", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_v",
        provider_choice="byo_key",
        byo_provider=None,
        owner_type="personal",
    )
    row = await billing_repo.get_by_owner_id("user_v")
    assert row["provider_choice"] == "byo_key"
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_set_provider_choice_overwrites_byo_provider_when_switching_away(dynamodb_table):
    """Switching from byo_key to bedrock_claude should clear byo_provider on the row."""
    await billing_repo.create_if_not_exists("user_t", "cus_yz1", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_t",
        provider_choice="byo_key",
        byo_provider="openai",
        owner_type="personal",
    )
    await billing_repo.set_provider_choice(
        "user_t",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="personal",
    )
    row = await billing_repo.get_by_owner_id("user_t")
    assert row["provider_choice"] == "bedrock_claude"
    assert "byo_provider" not in row or row["byo_provider"] is None


@pytest.mark.asyncio
async def test_clear_provider_choice(dynamodb_table):
    await billing_repo.create_if_not_exists("user_s", "cus_234", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_s",
        provider_choice="byo_key",
        byo_provider="openai",
        owner_type="personal",
    )
    await billing_repo.clear_provider_choice("user_s")
    row = await billing_repo.get_by_owner_id("user_s")
    assert row.get("provider_choice") is None
    assert row.get("byo_provider") is None


@pytest.mark.asyncio
async def test_clear_provider_choice_no_row_is_noop(dynamodb_table):
    """clear_provider_choice must NOT create a phantom billing row when
    called for an owner without one. Otherwise
    /billing/trial-checkout's `if not account` check sees the phantom and
    skips Stripe customer creation."""
    await billing_repo.clear_provider_choice("user_does_not_exist")
    row = await billing_repo.get_by_owner_id("user_does_not_exist")
    assert row is None  # no phantom row created


@pytest.mark.asyncio
async def test_clear_provider_choice_existing_row_clears_fields(dynamodb_table):
    """Sanity: when the row exists, clear still works."""
    await billing_repo.create_if_not_exists("user_y2", "cus_xyz", owner_type="personal")
    await billing_repo.set_provider_choice(
        "user_y2",
        provider_choice="byo_key",
        byo_provider="openai",
        owner_type="personal",
    )
    await billing_repo.clear_provider_choice("user_y2")
    row = await billing_repo.get_by_owner_id("user_y2")
    assert row is not None  # row preserved
    assert row.get("provider_choice") is None
    assert row.get("byo_provider") is None
    assert row.get("stripe_customer_id") == "cus_xyz"  # original fields preserved
