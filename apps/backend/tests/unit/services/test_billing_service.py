"""Tests for BillingService — flat-fee model."""

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
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
async def test_build_subscription_checkout_passes_line_items_verbatim(monkeypatch):
    """The builder forwards its line_items list to Stripe untouched — this is
    what lets the team path add a base-price item alongside the seat item in
    a later task without the builder itself changing."""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return MagicMock(id="cs_test_1", url="https://checkout.stripe.com/x")

    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )

    items = [{"price": "price_base", "quantity": 1}, {"price": "price_seat", "quantity": 3}]
    await billing_service._build_subscription_checkout(
        owner_id="org_1",
        line_items=items,
        metadata={"owner_id": "org_1"},
        success_url="https://x/ok",
        cancel_url="https://x/no",
        idempotency_key="k1",
    )

    assert captured["line_items"] == items


class TestLegacyFlatCheckoutIdempotencyKey:
    """The retained legacy builder uses one stable key per 5-minute bucket."""

    # Frozen so the 5-minute bucket cannot roll between two calls and fake a
    # difference (or hide a sameness).
    NOW = 1_800_000_000.0

    @classmethod
    async def _key_for(cls) -> str:
        created = MagicMock(return_value=MagicMock(id="cs_1"))
        with (
            patch("core.services.billing_service.time.time", return_value=cls.NOW),
            patch.object(
                billing_service.billing_repo,
                "get_by_owner_id",
                new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
            ),
            patch.object(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_flat"),
            patch("stripe.checkout.Session.create", new=created),
        ):
            await billing_service._build_subscription_checkout(
                owner_id="u_1",
                line_items=[{"price": "price_flat", "quantity": 1}],
                metadata={"owner_id": "u_1"},
                success_url="https://x/ok",
                cancel_url="https://x/no",
                idempotency_key=billing_service._checkout_idempotency_key("flat_checkout", "u_1", "bedrock_claude:_"),
            )
        return created.call_args.kwargs["idempotency_key"]

    @pytest.mark.asyncio
    async def test_flat_checkout_key_is_untouched(self):
        """The current flat-checkout key stays stable within its time bucket."""
        key = await self._key_for()
        with patch("core.services.billing_service.time.time", return_value=self.NOW):
            unchanged = billing_service._checkout_idempotency_key("flat_checkout", "u_1", "bedrock_claude:_")
        assert key == unchanged
