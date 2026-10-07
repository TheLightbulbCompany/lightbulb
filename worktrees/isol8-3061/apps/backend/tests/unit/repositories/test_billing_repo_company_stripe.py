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


@pytest.mark.asyncio
async def test_set_company_stripe_account_binds_once(billing_table):
    from core.repositories import billing_repo

    row = await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    assert row["company_stripe_account_id"] == "acct_1"
    assert row["company_stripe_source"] == "created"
    assert row["plan"] == "flat"  # untouched field survives the diff write

    again = await billing_repo.set_company_stripe_account("org_a", account_id="acct_2", source="connected")
    assert again is None
    assert billing_table.get_item(Key={"owner_id": "org_a"})["Item"]["company_stripe_account_id"] == "acct_1"


@pytest.mark.asyncio
async def test_set_company_stripe_account_no_row_is_none(billing_table):
    from core.repositories import billing_repo

    assert await billing_repo.set_company_stripe_account("org_missing", account_id="acct_1", source="created") is None


@pytest.mark.asyncio
async def test_status_update_requires_matching_account(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    row = await billing_repo.set_company_stripe_status(
        "org_a", account_id="acct_1", details_submitted=True, charges_enabled=True, payouts_enabled=False
    )
    assert row["company_stripe_charges_enabled"] is True
    assert row["company_stripe_payouts_enabled"] is False

    stale = await billing_repo.set_company_stripe_status(
        "org_a", account_id="acct_other", details_submitted=True, charges_enabled=True, payouts_enabled=True
    )
    assert stale is None


@pytest.mark.asyncio
async def test_get_by_company_stripe_account_id(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="connected")
    found = await billing_repo.get_by_company_stripe_account_id("acct_1")
    assert found["owner_id"] == "org_a"
    assert await billing_repo.get_by_company_stripe_account_id("acct_nope") is None


@pytest.mark.asyncio
async def test_clear_company_stripe_account(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
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
    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="connected")

    assert await billing_repo.list_company_stripe_owners() == ["org_a"]


@pytest.mark.asyncio
async def test_clearing_the_company_account_drops_the_project_pointer(billing_table):
    """Deauthorization unbinds everything company-Stripe, the Project included:
    a pointer into an account we can no longer reach is worse than none."""
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    await billing_repo.set_company_stripe_project_id("org_a", project_id="proj_1")
    row = await billing_repo.clear_company_stripe_account("org_a", account_id="acct_1")
    assert "company_stripe_project_id" not in row
