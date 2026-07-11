"""Tests for billing account DynamoDB repository."""

import os
import time
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
async def test_set_subscription_clear_seat_fields_removes_stale_seat_data(dynamodb_table):
    """Regression: cancelling a per-seat org subscription must also clear
    subscription_item_id + seat_count, not just the subscription id/status —
    otherwise is_seat_subscription (routers/billing.py) stays True forever."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat_cancel", "cus_seat_cancel", owner_type="org")
    await billing_repo.set_team_subscription(
        "org_seat_cancel",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=4,
    )

    result = await billing_repo.set_subscription(
        owner_id="org_seat_cancel",
        subscription_id=None,
        status="canceled",
        trial_end=None,
        clear_seat_fields=True,
    )
    assert result is not None
    assert result["subscription_status"] == "canceled"
    assert result["stripe_subscription_id"] is None
    assert "subscription_item_id" not in result
    assert "seat_count" not in result

    stored = await billing_repo.get_by_owner_id("org_seat_cancel")
    assert "subscription_item_id" not in stored
    assert "seat_count" not in stored


@pytest.mark.asyncio
async def test_set_subscription_without_clear_seat_fields_preserves_seat_data(dynamodb_table):
    """Default behavior (clear_seat_fields=False) — used by the
    created/updated webhook path — must NOT touch seat fields, since that
    path relies on set_team_subscription running separately/afterward."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat_keep", "cus_seat_keep", owner_type="org")
    await billing_repo.set_team_subscription(
        "org_seat_keep",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=3,
    )

    result = await billing_repo.set_subscription(
        owner_id="org_seat_keep",
        subscription_id="sub_seat",
        status="active",
        trial_end=None,
    )
    assert result["subscription_item_id"] == "si_seat"
    assert result["seat_count"] == 3


@pytest.mark.asyncio
async def test_set_team_subscription_writes_item_id_and_seats(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat", "cus_seat", owner_type="org")
    result = await billing_repo.set_team_subscription(
        "org_seat",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=5,
    )
    assert result is not None
    assert result["stripe_subscription_id"] == "sub_seat"
    assert result["subscription_item_id"] == "si_seat"
    assert result["subscription_status"] == "active"
    assert result["seat_count"] == 5

    stored = await billing_repo.get_by_owner_id("org_seat")
    assert stored["subscription_item_id"] == "si_seat"
    assert stored["seat_count"] == 5


@pytest.mark.asyncio
async def test_set_team_subscription_noops_without_row(dynamodb_table):
    from core.repositories import billing_repo

    # Never upsert a phantom row for an owner with no billing account.
    result = await billing_repo.set_team_subscription(
        "org_missing",
        subscription_id="sub_x",
        subscription_item_id="si_x",
        status="active",
        seat_count=2,
    )
    assert result is None
    assert await billing_repo.get_by_owner_id("org_missing") is None


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
    await billing_repo.set_metronome_ids(
        "org_m", metronome_customer_id="cust-uuid-1", metronome_contract_id="contract-1"
    )

    item = await billing_repo.get_by_owner_id("org_m")
    assert item["metronome_customer_id"] == "cust-uuid-1"
    assert item["metronome_contract_id"] == "contract-1"

    # The webhook resolves owner via the Metronome UUID.
    resolved = await billing_repo.get_by_metronome_customer_id("cust-uuid-1")
    assert resolved is not None
    assert resolved["owner_id"] == "org_m"

    assert await billing_repo.get_by_metronome_customer_id("unknown-uuid") is None


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_writes_trial_row(dynamodb_table):
    from core.repositories import billing_repo

    before = int(time.time())
    await billing_repo.create_trial_local_if_absent("user_trial_1", "personal")
    after = int(time.time())

    item = await billing_repo.get_by_owner_id("user_trial_1")
    assert item is not None
    assert item["subscription_status"] == "trial_local"
    assert item["owner_type"] == "personal"
    assert "trial_end" in item
    three_days = 3 * 24 * 60 * 60
    assert before + three_days <= item["trial_end"] <= after + three_days
    assert not any(k.startswith("stripe_") for k in item)


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_is_noop_when_row_exists(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_trial_2", "personal")
    first = await billing_repo.get_by_owner_id("user_trial_2")

    # Second call must not overwrite the existing row.
    await billing_repo.create_trial_local_if_absent("user_trial_2", "org")
    second = await billing_repo.get_by_owner_id("user_trial_2")
    assert second == first


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_noop_on_existing_active_row(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_trial_3", "cus_active")
    await billing_repo.set_subscription(
        owner_id="user_trial_3",
        subscription_id="sub_active",
        status="active",
    )
    before = await billing_repo.get_by_owner_id("user_trial_3")

    await billing_repo.create_trial_local_if_absent("user_trial_3", "personal")
    after = await billing_repo.get_by_owner_id("user_trial_3")

    assert after == before
    assert after["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_set_metronome_ids_requires_existing_row(dynamodb_table):
    """The conditional write must not upsert a phantom billing row (same trap
    documented on clear_provider_choice)."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.set_metronome_ids(
            "org_ghost", metronome_customer_id="cust-uuid-1", metronome_contract_id="contract-1"
        )
    assert await billing_repo.get_by_owner_id("org_ghost") is None
