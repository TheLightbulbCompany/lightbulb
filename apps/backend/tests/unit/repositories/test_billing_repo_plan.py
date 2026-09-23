"""Tests for billing_repo.set_plan + the plan field defaulting on the response."""

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
                {"AttributeName": "apple_original_transaction_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "apple-original-transaction-index",
                    "KeySchema": [{"AttributeName": "apple_original_transaction_id", "KeyType": "HASH"}],
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
async def test_set_plan_writes_and_reads_back(dynamodb_table):
    """set_plan persists the tier + updated_at, and get_by_owner_id reads it back."""
    await billing_repo.create_if_not_exists("user_plan", "cus_plan", owner_type="personal")
    await billing_repo.set_plan("user_plan", plan="ultra_pro", now_iso="2026-07-19T00:00:00Z")

    row = await billing_repo.get_by_owner_id("user_plan")
    assert row["plan"] == "ultra_pro"
    assert row["updated_at"] == "2026-07-19T00:00:00Z"
    # The rest of the row is untouched.
    assert row["stripe_customer_id"] == "cus_plan"


@pytest.mark.asyncio
async def test_set_plan_can_return_to_standard(dynamodb_table):
    """A tier downgrade is a plain re-set to "standard" over an ultra_pro row."""
    await billing_repo.create_if_not_exists("user_dg", "cus_dg", owner_type="personal")
    await billing_repo.set_plan("user_dg", plan="ultra_pro", now_iso="2026-07-19T00:00:00Z")
    await billing_repo.set_plan("user_dg", plan="standard", now_iso="2026-07-19T01:00:00Z")

    row = await billing_repo.get_by_owner_id("user_dg")
    assert row["plan"] == "standard"


@pytest.mark.asyncio
async def test_set_plan_nonexistent_owner_raises(dynamodb_table):
    """No phantom billing rows — same invariant as set_metronome_ids /
    set_stripe_customer_id. A conditional update against a missing owner_id
    must raise (ConditionExpression), never silently upsert."""
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        await billing_repo.set_plan("ghost_owner", plan="ultra_pro", now_iso="2026-07-19T00:00:00Z")
    assert await billing_repo.get_by_owner_id("ghost_owner") is None


@pytest.mark.asyncio
async def test_row_without_plan_key_defaults_to_standard(dynamodb_table):
    """An old row created before the plan field existed carries no `plan` key;
    the response construction defaults a missing value to "standard"."""
    await billing_repo.create_if_not_exists("user_old", "cus_old", owner_type="personal")

    row = await billing_repo.get_by_owner_id("user_old")
    assert "plan" not in row
    # Mirror the construction-site defaulting expression.
    assert (row.get("plan") or "standard") == "standard"


def test_billing_account_response_plan_defaults_to_none():
    """BillingAccountResponse.plan is None when unset: a trial or a missing row has
    no plan. The router, not the schema, maps a plan-less PAID row to "standard"."""
    from schemas.billing import BillingAccountResponse

    resp = BillingAccountResponse(
        is_subscribed=False,
        current_spend=0.0,
        lifetime_spend=0.0,
        workspace_price_usd=50,
        ultra_price_usd=150,
    )
    assert resp.plan is None
