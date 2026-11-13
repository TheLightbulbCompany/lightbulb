import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def billing_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
                {"AttributeName": "company_stripe_account_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "company-stripe-account-index",
                    "KeySchema": [{"AttributeName": "company_stripe_account_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", client):
            table.put_item(
                Item={"owner_id": "org_a", "owner_type": "org", "stripe_customer_id": "cus_a", "plan": "flat"}
            )
            yield table


def _bind(table, owner_id: str, account_id: str, source: str) -> None:
    """A row bound to a company Stripe account (no code path binds one any more; old rows remain)."""
    table.update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="SET company_stripe_account_id = :a, company_stripe_source = :s",
        ExpressionAttributeValues={":a": account_id, ":s": source},
    )


@pytest.mark.asyncio
async def test_clear_company_stripe_account(billing_table):
    from core.repositories import billing_repo

    _bind(billing_table, "org_a", "acct_1", "created")
    assert await billing_repo.clear_company_stripe_account("org_a", account_id="acct_zzz") is None
    row = await billing_repo.clear_company_stripe_account("org_a", account_id="acct_1")
    assert "company_stripe_account_id" not in row
    assert "company_stripe_source" not in row


@pytest.mark.asyncio
async def test_set_company_stripe_project_id_stores_once(billing_table):
    """One Provisioning Project per company: the conditional write is the
    arbiter when two provisioning workers race to create it."""
    from core.repositories import billing_repo

    row = await billing_repo.set_company_stripe_project_id("org_a", project_id="proj_1")
    assert row["company_stripe_project_id"] == "proj_1"
    assert row["plan"] == "flat"  # untouched field survives the diff write

    assert await billing_repo.set_company_stripe_project_id("org_a", project_id="proj_2") is None
    assert billing_table.get_item(Key={"owner_id": "org_a"})["Item"]["company_stripe_project_id"] == "proj_1"
    assert await billing_repo.set_company_stripe_project_id("org_missing", project_id="proj_1") is None


@pytest.mark.asyncio
async def test_list_company_stripe_owners_only_bound_rows(billing_table):
    from core.repositories import billing_repo

    billing_table.put_item(Item={"owner_id": "org_unbound", "owner_type": "org", "plan": "flat"})
    _bind(billing_table, "org_a", "acct_1", "connected")

    assert await billing_repo.list_company_stripe_owners() == ["org_a"]


@pytest.mark.asyncio
async def test_clearing_the_company_account_drops_the_project_pointer(billing_table):
    """Deauthorization unbinds everything company-Stripe, the Project included:
    a pointer into an account we can no longer reach is worse than none."""
    from core.repositories import billing_repo

    _bind(billing_table, "org_a", "acct_1", "created")
    await billing_repo.set_company_stripe_project_id("org_a", project_id="proj_1")
    row = await billing_repo.clear_company_stripe_account("org_a", account_id="acct_1")
    assert "company_stripe_project_id" not in row
