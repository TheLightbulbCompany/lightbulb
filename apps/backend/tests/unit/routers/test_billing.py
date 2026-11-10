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
        monkeypatch.setattr("core.config.settings.STRIPE_WORKSPACE_PRICE_ID", "price_workspace_test")
        yield


class TestGetBillingAccount:
    """GET /api/v1/billing/account — flat-fee response shape."""

    @pytest.mark.asyncio
    @patch("routers.billing.get_usage_summary")
    @patch("routers.billing.billing_repo")
    async def test_get_billing_account_active_subscription(self, mock_repo, mock_get_summary, async_client):
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_test_456",
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
    async def test_account_reports_front_desk_flag(self, mock_repo, mock_get_summary, async_client, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
        mock_get_summary.return_value = {"total_spend": 0, "lifetime_spend": 0}
        row = {"owner_id": "org_test_456", "subscription_status": "active"}
        mock_repo.get_by_owner_id = AsyncMock(return_value={**row, "front_desk": True})
        data = (await async_client.get("/api/v1/billing/account")).json()
        assert data["front_desk"] is True
        assert data["front_desk_price_usd"] == 250
        monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "")
        assert (await async_client.get("/api/v1/billing/account")).json()["front_desk_price_usd"] is None
        mock_repo.get_by_owner_id = AsyncMock(return_value=row)
        assert (await async_client.get("/api/v1/billing/account")).json()["front_desk"] is False

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
                "owner_id": "org_test_456",
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


class TestGetUsage:
    @pytest.mark.asyncio
    @patch("routers.billing.usage_repo")
    @patch("routers.billing.get_usage_summary")
    async def test_get_usage_returns_summary(self, mock_get_summary, mock_usage_repo, async_client):
        mock_usage_repo.get_member_usage = AsyncMock(return_value=[])
        mock_get_summary.return_value = {
            "period": "2026-03",
            "total_spend": 5.25,
            "total_input_tokens": 10000,
            "total_output_tokens": 5000,
            "total_cache_read_tokens": 200,
            "total_cache_write_tokens": 100,
            "request_count": 42,
            "lifetime_spend": 15.75,
        }

        response = await async_client.get("/api/v1/billing/usage")
        assert response.status_code == 200
        data = response.json()
        assert data["total_spend"] == 5.25
        assert data["request_count"] == 42
        assert data["lifetime_spend"] == 15.75
        assert data["by_member"] == []

    @pytest.mark.asyncio
    @patch("routers.billing._resolve_clerk_user", new_callable=AsyncMock)
    @patch("routers.billing.usage_repo")
    @patch("routers.billing.get_usage_summary")
    async def test_owner_attributed_usage_is_named_not_a_raw_org_id(
        self, mock_get_summary, mock_usage_repo, mock_clerk, async_client
    ):
        row = {"total_spend_microdollars": 0, "total_input_tokens": 1, "total_output_tokens": 1, "request_count": 1}
        row |= {"capture_prompt_tokens": 0, "capture_completion_tokens": 0}
        mock_usage_repo.get_member_usage = AsyncMock(
            return_value=[{"user_id": "org_test_456", **row}, {"user_id": "user_test_123", **row}]
        )
        mock_get_summary.return_value = {
            "period": "2026-09",
            "total_spend": 0,
            "total_input_tokens": 2,
            "total_output_tokens": 2,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "request_count": 2,
            "lifetime_spend": 0,
        }
        mock_clerk.return_value = {"display_name": "Ada", "email": "ada@example.com"}

        members = (await async_client.get("/api/v1/billing/usage")).json()["by_member"]
        assert [m["display_name"] for m in members] == ["Workspace agents & automations", "Ada"]
        mock_clerk.assert_awaited_once_with("user_test_123")


class TestGetMyUsage:
    @pytest.mark.asyncio
    @patch("routers.billing.usage_repo")
    async def test_get_my_usage_with_data(self, mock_usage_repo, async_client):
        mock_usage_repo.get_period_usage = AsyncMock(
            return_value={
                "total_spend_microdollars": 3_500_000,
                "total_input_tokens": 8000,
                "total_output_tokens": 4000,
                "total_cache_read_tokens": 100,
                "total_cache_write_tokens": 50,
                "request_count": 25,
            }
        )

        response = await async_client.get("/api/v1/billing/my-usage")
        assert response.status_code == 200
        data = response.json()
        assert data["total_spend"] == 3.5
        assert data["request_count"] == 25
        call_args = mock_usage_repo.get_period_usage.call_args
        assert call_args[0][0] == "org_test_456"
        assert call_args[0][1].startswith("member:user_test_123:")

    @pytest.mark.asyncio
    @patch("routers.billing.usage_repo")
    async def test_get_my_usage_no_data(self, mock_usage_repo, async_client):
        mock_usage_repo.get_period_usage = AsyncMock(return_value=None)

        response = await async_client.get("/api/v1/billing/my-usage")
        assert response.status_code == 200
        data = response.json()
        assert data["total_spend"] == 0.0
        assert data["request_count"] == 0


class TestStripeWebhook:
    @pytest.fixture(autouse=True)
    def _no_front_desk_sync(self, monkeypatch):
        """The Front desk line sync reaches Stripe and DynamoDB; these tests are not about it."""
        monkeypatch.setattr("core.services.text_lines.sync_front_desk", AsyncMock(return_value="absent"))

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
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_subscription_updated_persists_status_and_trial_end(
        self, mock_stripe, mock_repo, async_client, dedup_table_and_settings
    ):
        """customer.subscription.updated must call set_subscription with
        status + trial_end so the frontend trial banner stays fresh."""
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_update_x",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_update_x",
                    "customer": "cus_update_x",
                    "status": "trialing",
                    "trial_end": 1700000000,
                    "items": {
                        "data": [
                            {
                                "id": "si_workspace",
                                "price": {
                                    "id": "price_workspace_test",
                                    "recurring": {"interval": "month"},
                                },
                            }
                        ]
                    },
                }
            },
        }
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[
                {
                    "owner_id": "user_update_x",
                    "stripe_customer_id": "cus_update_x",
                }
            ]
        )
        mock_repo.set_subscription = AsyncMock()

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )

        assert response.status_code == 200
        mock_repo.set_subscription.assert_awaited_once()
        kwargs = mock_repo.set_subscription.await_args.kwargs
        assert kwargs.get("owner_id") == "user_update_x"
        assert kwargs.get("subscription_id") == "sub_update_x"
        assert kwargs.get("status") == "trialing"
        assert kwargs.get("trial_end") == 1700000000

    @pytest.mark.asyncio
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_subscription_created_persists_status_and_provider_choice(
        self, mock_stripe, mock_repo, async_client, dedup_table_and_settings
    ):
        """customer.subscription.created (the FIRST event for a new trial)
        must persist subscription_status + trial_end + provider_choice — same
        as customer.subscription.updated. Without this branch, users finishing
        Stripe Checkout are stuck on the provider picker because is_subscribed
        stays False.

        provider_choice is persisted on the *billing_accounts* row keyed
        by ``account["owner_id"]``. The webhook is an idempotent backup
        for /trial-checkout's synchronous write.
        """
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_created_x",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_created_x",
                    "customer": "cus_created_x",
                    "status": "trialing",
                    "trial_end": 1700000000,
                    "metadata": {
                        "provider_choice": "chatgpt_oauth",
                        "clerk_user_id": "user_clerk_x",
                    },
                    "items": {
                        "data": [
                            {
                                "id": "si_workspace",
                                "price": {
                                    "id": "price_workspace_test",
                                    "recurring": {"interval": "month"},
                                },
                            }
                        ]
                    },
                }
            },
        }
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[
                {
                    "owner_id": "org_x",
                    "owner_type": "org",
                    "stripe_customer_id": "cus_created_x",
                }
            ]
        )
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_x",
                "owner_type": "org",
                "stripe_customer_id": "cus_created_x",
            }
        )
        mock_repo.set_subscription = AsyncMock()
        mock_repo.set_provider_choice = AsyncMock()

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )

        assert response.status_code == 200
        mock_repo.set_subscription.assert_awaited_once()
        kwargs = mock_repo.set_subscription.await_args.kwargs
        assert kwargs["owner_id"] == "org_x"
        assert kwargs["subscription_id"] == "sub_created_x"
        assert kwargs["status"] == "trialing"
        assert kwargs["trial_end"] == 1700000000
        # Webhook backup writes provider_choice to billing_repo, keyed on
        # account["owner_id"] (NOT clerk_user_id, NOT user_repo). Metadata is
        # unauthenticated, so connector_user_id is None despite the
        # clerk_user_id present in metadata.
        mock_repo.set_provider_choice.assert_awaited_once_with(
            "org_x",
            provider_choice="chatgpt_oauth",
            byo_provider=None,
            connector_user_id=None,
        )

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
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_subscription_updated_resolves_owner_via_metadata(
        self, mock_stripe, mock_repo, async_client, dedup_table_and_settings
    ):
        """When subscription.metadata.owner_id is set, owner resolution must
        prefer it over the stripe_customer_id GSI — that's what makes
        email-shared customers (one human, multiple billing rows) safe."""
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_meta_owner",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_meta",
                    "customer": "cus_shared",
                    "status": "active",
                    "metadata": {"owner_id": "org_xyz"},
                    "items": {
                        "data": [
                            {
                                "id": "si_workspace",
                                "price": {
                                    "id": "price_workspace_test",
                                    "recurring": {"interval": "month"},
                                },
                            }
                        ]
                    },
                }
            },
        }
        mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_xyz", "stripe_customer_id": "cus_shared"})
        # Even if the customer GSI would return a different (wrong) row,
        # metadata.owner_id wins. Assert the GSI lookup was never reached.
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[
                {"owner_id": "wrong_personal", "stripe_customer_id": "cus_shared"},
                {"owner_id": "org_xyz", "stripe_customer_id": "cus_shared"},
            ]
        )
        mock_repo.set_subscription = AsyncMock()

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )

        assert response.status_code == 200
        mock_repo.list_by_stripe_customer_id.assert_not_called()
        mock_repo.set_subscription.assert_awaited_once()
        assert mock_repo.set_subscription.await_args.kwargs["owner_id"] == "org_xyz"

    @pytest.mark.asyncio
    @patch("core.services.stripe_webhook_handlers.billing_repo")
    @patch("routers.billing.stripe")
    async def test_subscription_updated_disambiguates_shared_customer_by_sub_id(
        self, mock_stripe, mock_repo, async_client, dedup_table_and_settings
    ):
        """Legacy subscriptions (no metadata.owner_id) on a now-shared
        Stripe customer must be disambiguated by stripe_subscription_id,
        not silently picked from the GSI's first hit."""
        mock_stripe.Webhook.construct_event.return_value = {
            "id": "evt_legacy_shared",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_legacy_org",
                    "customer": "cus_shared",
                    "status": "active",
                    "items": {
                        "data": [
                            {
                                "id": "si_workspace",
                                "price": {
                                    "id": "price_workspace_test",
                                    "recurring": {"interval": "month"},
                                },
                            }
                        ]
                    },
                }
            },
        }
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_repo.list_by_stripe_customer_id = AsyncMock(
            return_value=[
                {
                    "owner_id": "personal_user",
                    "stripe_customer_id": "cus_shared",
                    "stripe_subscription_id": "sub_legacy_personal",
                },
                {
                    "owner_id": "org_legacy",
                    "stripe_customer_id": "cus_shared",
                    "stripe_subscription_id": "sub_legacy_org",
                },
            ]
        )
        mock_repo.set_subscription = AsyncMock()

        response = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=b'{"test": true}',
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )

        assert response.status_code == 200
        mock_repo.set_subscription.assert_awaited_once()
        assert mock_repo.set_subscription.await_args.kwargs["owner_id"] == "org_legacy"


@pytest.mark.asyncio
async def test_public_prices_need_no_sign_in_and_match_checkout(async_client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    response = await async_client.get("/api/v1/billing/prices")
    assert response.status_code == 200
    assert response.json() == {
        "workspace_price_usd": settings.WORKSPACE_PRICE_USD,
        "included_members": 5,
        "seat_price_usd": settings.SEAT_PRICE_USD,
        "trial_days": 30,
        "managed_markup": settings.OPENROUTER_MARKUP or None,
        "front_desk_price_usd": settings.FRONT_DESK_PRICE_USD,
        "voice_plan_monthly_usd": 250,
        "voice_included_minutes": 1500,
        "voice_overage_usd_per_minute": 0.35,
    }


@pytest.mark.asyncio
async def test_prices_include_front_desk(async_client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    response = await async_client.get("/api/v1/billing/prices")
    assert response.json()["front_desk_price_usd"] == 250


@pytest.mark.asyncio
async def test_public_prices_have_no_front_desk_price_while_unconfigured(async_client, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "")
    response = await async_client.get("/api/v1/billing/prices")
    assert response.json()["front_desk_price_usd"] is None
