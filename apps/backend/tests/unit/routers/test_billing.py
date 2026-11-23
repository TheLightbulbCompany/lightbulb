"""Tests for billing API endpoints — flat-fee model."""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _stub_reconcile_dispatch(monkeypatch):
    """Live subscription events dispatch a durable reconcile (a Step Functions execution), and a
    dispatch that cannot start raises so Stripe redelivers. Stub it so no test reaches AWS."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    return dispatch


@pytest.fixture
def mock_auth_context(mock_org_admin_context):
    """Billing routes default to an active organization workspace."""
    return mock_org_admin_context


@pytest.fixture
def dedup_table_and_settings(monkeypatch):
    """Provision a moto-mocked dedup table and point the env var at it."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-webhook-event-dedup",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")
        yield


class TestGetBillingAccount:
    """GET /api/v1/billing/account — flat-fee response shape."""

    @pytest.mark.asyncio
    @patch("routers.billing.get_usage_summary")
    @patch("routers.billing.billing_repo")
    async def test_get_billing_account_active_subscription(self, mock_repo, mock_get_summary, async_client):
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "stripe_customer_id": "cus_billing_test",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "active",
                "trial_end": None,
            }
        )
        mock_get_summary.return_value = {
            "period": "2026-04",
            "total_spend": 3.50,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "request_count": 0,
            "lifetime_spend": 12.0,
        }

        response = await async_client.get("/api/v1/billing/account")
        assert response.status_code == 200
        data = response.json()
        assert data["is_subscribed"] is True
        assert data["current_spend"] == 3.50
        assert data["lifetime_spend"] == 12.0
        assert data["subscription_status"] == "active"
        assert data["trial_end"] is None

    @pytest.mark.asyncio
    @patch("routers.billing.get_usage_summary")
    @patch("routers.billing.billing_repo")
    async def test_get_billing_account_trial_local_is_not_subscribed(self, mock_repo, mock_get_summary, async_client):
        """The no-card free tier grants platform use but is NOT a paid
        subscription — ``is_subscribed`` keys off ``is_paid_subscription``,
        so a trial_local row must report False (the billing UI renders the
        free-tier card with a Subscribe CTA, never "Active subscription")."""
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "subscription_status": "trial_local",
                "trial_end": None,
            }
        )
        mock_get_summary.return_value = {
            "period": "2026-07",
            "total_spend": 0.25,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "request_count": 3,
            "lifetime_spend": 0.25,
        }

        response = await async_client.get("/api/v1/billing/account")
        assert response.status_code == 200
        data = response.json()
        assert data["is_subscribed"] is False
        assert data["subscription_status"] == "trial_local"

    @pytest.mark.asyncio
    @patch("routers.billing.get_usage_summary")
    @patch("routers.billing.billing_repo")
    @patch("core.services.billing_service.stripe")
    @patch("core.services.billing_service.billing_repo")
    async def test_get_billing_account_no_row_returns_pre_signup_defaults(
        self, mock_svc_repo, mock_stripe, mock_router_repo, mock_get_summary, async_client
    ):
        """No billing row = pre-signup user. Endpoint returns the empty
        response without auto-creating a Stripe customer or DDB row."""
        mock_router_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_get_summary.return_value = {
            "period": "2026-04",
            "total_spend": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "request_count": 0,
            "lifetime_spend": 0,
        }

        response = await async_client.get("/api/v1/billing/account")
        assert response.status_code == 200
        data = response.json()
        assert data["is_subscribed"] is False
        assert data["subscription_status"] is None
        assert data["trial_end"] is None

        mock_stripe.Customer.create.assert_not_called()
        mock_svc_repo.create_if_not_exists.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selling", [False, True])
    @patch("routers.billing.get_usage_summary")
    @patch("routers.billing.billing_repo")
    async def test_apple_product_ids_are_listed_only_while_selling(
        self, mock_repo, mock_get_summary, selling, async_client, monkeypatch
    ):
        from core.services import apple_iap

        monkeypatch.setattr("routers.billing.settings.APPLE_IAP_SELLING", selling)
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_get_summary.return_value = {"total_spend": 0, "lifetime_spend": 0}

        response = await async_client.get("/api/v1/billing/account")
        assert response.status_code == 200
        expected = sorted(apple_iap.SELLING_PRODUCTS) if selling else []
        assert sorted(response.json()["apple_iap_product_ids"]) == expected


class TestStripeWebhook:
    @pytest.mark.asyncio
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_subscription_deleted_webhook(self, mock_stripe, mock_repo, async_client, dedup_table_and_settings):
        """customer.subscription.deleted: cancel + tear down container."""
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_deleted_test_1",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_test_123",
                    "customer": "cus_webhook_test",
                }
            },
        }
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[
                {
                    "owner_id": "user_webhook_test",
                    "stripe_customer_id": "cus_webhook_test",
                }
            ]
        )

        with (
            patch("routers.billing.BillingService") as mock_billing_cls,
            # Teardown asks whether ANY provider still pays before running, so
            # it re-reads the row — Stripe ending is no longer the same question
            # as the entitlement ending. Nothing else pays here.
            patch(
                "core.services.entitlement_teardown.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value={"owner_id": "user_webhook_test", "subscription_status": "canceled"}),
            ),
        ):
            mock_billing_svc = AsyncMock()
            mock_billing_cls.return_value = mock_billing_svc

            response = await async_client.post(
                "/api/v1/billing/webhooks/stripe",
                content=b'{"test": true}',
                headers={"stripe-signature": "test_sig", "content-type": "application/json"},
            )
        assert response.status_code == 200
        mock_billing_svc.cancel_subscription.assert_called_once()

    @pytest.mark.asyncio
    @patch("core.services.stripe_webhook_handlers.put_metric")
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_trial_will_end_emits_metric(
        self, mock_stripe, mock_repo, mock_put_metric, async_client, dedup_table_and_settings
    ):
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_trial_x",
            "type": "customer.subscription.trial_will_end",
            "data": {
                "object": {
                    "id": "sub_trial_x",
                    "customer": "cus_trial_x",
                    "trial_end": 1700000000,
                    "status": "trialing",
                }
            },
        }
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[{"owner_id": "user_trial_x", "stripe_customer_id": "cus_trial_x"}]
        )

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )
        assert response.status_code == 200
        metric_names = [c.args[0] for c in mock_put_metric.call_args_list]
        assert "trial.will_end_3day" in metric_names

    @pytest.mark.asyncio
    @patch("routers.billing.stripe")
    async def test_webhook_invalid_signature(self, mock_stripe, async_client):
        mock_stripe.Webhook.construct_event.side_effect = Exception("bad sig")

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "bad_sig", "content-type": "application/json"},
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_public_prices_need_no_sign_in(async_client):
    response = await async_client.get("/api/v1/billing/prices")
    assert response.status_code == 200
    assert response.json() == {
        "voice_plan_monthly_usd": None,
        "voice_included_minutes": None,
        "voice_overage_usd_per_minute": None,
    }


@pytest.mark.asyncio
async def test_public_prices_advertise_the_voice_plan_only_when_it_can_be_bought(async_client, monkeypatch):
    from core.billing import voice_plan

    monkeypatch.setattr(voice_plan, "available", lambda: True)
    body = (await async_client.get("/api/v1/billing/prices")).json()
    # The same numbers /voice-plan bills and the checkout acceptance pins.
    assert (body["voice_plan_monthly_usd"], body["voice_included_minutes"], body["voice_overage_usd_per_minute"]) == (
        250,
        1500,
        0.35,
    )


def test_checkout_acceptance_pins_the_billed_constants():
    from typing import get_args

    from core.billing import voice_billing
    from routers.billing import VoicePlanEnrollment

    f = VoicePlanEnrollment.model_fields
    assert get_args(f["acceptMonthlyUsd"].annotation) == (voice_billing.MONTHLY_USD,)
    assert get_args(f["acceptIncludedMinutes"].annotation) == (voice_billing.INCLUDED_MINUTES,)
    assert get_args(f["acceptOverageUsdPerMinute"].annotation) == (voice_billing.OVERAGE_USD_PER_MINUTE,)
