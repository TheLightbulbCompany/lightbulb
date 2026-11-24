"""Tests for BillingService — flat-fee model."""

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services import billing_service
from core.services.billing_service import (
    BillingService,
)


@pytest.fixture(autouse=True)
def _checkout_slot_claim_granted():
    """Checkout paths serialize session creation behind a
    per-owner DDB claim. Grant it by default so every existing checkout test
    exercises the path it was written for; the claim's own behavior is
    tested explicitly in TestCheckoutSlotClaim."""
    with (
        patch.object(
            billing_service.billing_repo,
            "try_claim_checkout_slot",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            billing_service.billing_repo,
            "release_checkout_slot",
            new=AsyncMock(),
        ),
        patch.object(
            billing_service.billing_repo,
            "set_pending_checkout",
            new=AsyncMock(),
        ),
    ):
        yield


@pytest.fixture
def dynamodb_billing_table():
    """Moto-backed billing-accounts table (mirrors
    tests/unit/repositories/test_billing_repo.py's fixture) for the one
    cancel_subscription test that needs real DynamoDB read-then-put
    semantics rather than a mocked billing_repo."""
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


class TestBillingServicePortal:
    @pytest.mark.asyncio
    @patch("core.services.billing_service.stripe")
    async def test_create_portal_session(self, mock_stripe):
        mock_stripe.billing_portal.Session.create.return_value = MagicMock(url="https://billing.stripe.com/test")
        url = await BillingService().create_portal_session(
            billing_account={"owner_id": "user_portal", "stripe_customer_id": "cus_portal"}
        )
        assert url == "https://billing.stripe.com/test"


class TestBillingServiceCancelSubscription:
    """Cancel writes status='canceled' and clears the subscription id via set_subscription."""

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    async def test_cancel_subscription(self, mock_repo):
        mock_repo.set_subscription = AsyncMock()
        await BillingService().cancel_subscription({"owner_id": "user_sub", "stripe_subscription_id": "sub_123"})
        mock_repo.set_subscription.assert_called_once_with(
            owner_id="user_sub",
            subscription_id=None,
            status="canceled",
            trial_end=None,
        )


@pytest.mark.asyncio
async def test_person_customer_does_not_reuse_another_tenants_email_customer(monkeypatch):
    """Voice-plan checkout makes the Stripe customer here: a new one per tenant, never one found by email."""
    service = BillingService()
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.billing_repo,
        "create_if_not_exists",
        AsyncMock(return_value={"owner_id": "user_2", "stripe_customer_id": "cus_user_2"}),
    )
    create = MagicMock(return_value=MagicMock(id="cus_user_2"))
    monkeypatch.setattr(billing_service.stripe.Customer, "create", create)
    list_customers = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "list", list_customers)

    account = await service.create_customer_for_owner(owner_id="user_2", email="owner@example.com")

    list_customers.assert_not_called()
    create.assert_called_once_with(
        metadata={"workspace_id": "user_2", "owner_id": "user_2"},
        idempotency_key="workspace-customer:v2:user_2",
    )
    assert account["stripe_customer_id"] == "cus_user_2"


@pytest.mark.asyncio
async def test_person_customer_backfill_race_keeps_the_existing_row_winner(monkeypatch):
    service = BillingService()
    existing = {"owner_id": "user_2", "subscription_status": "trial_local"}
    winner = {"owner_id": "user_2", "stripe_customer_id": "cus_winner"}
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(side_effect=[existing, winner]))
    monkeypatch.setattr(
        billing_service.billing_repo,
        "set_stripe_customer_id",
        AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already claimed"}},
                "UpdateItem",
            )
        ),
    )
    monkeypatch.setattr(billing_service.stripe.Customer, "create", MagicMock(return_value=MagicMock(id="cus_loser")))
    delete = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "delete", delete)

    account = await service.create_customer_for_owner(owner_id="user_2", email="owner@example.com")

    assert account == winner
    delete.assert_called_once_with("cus_loser", idempotency_key="delete_customer:cus_loser")
