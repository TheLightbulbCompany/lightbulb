"""Tests for billing API endpoints — flat-fee model."""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws


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


class TestGetUsage:
    @pytest.mark.asyncio
    @patch("routers.billing.get_usage_summary")
    async def test_get_usage_returns_summary(self, mock_get_summary, async_client):
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
        assert call_args[0][0] == "user_test_123"
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


class TestGetPricing:
    @pytest.mark.asyncio
    @patch("routers.billing.get_all_rates")
    async def test_get_pricing_returns_models_without_markup(self, mock_get_rates, async_client):
        """Flat-fee pricing endpoint: raw Bedrock prices in USD/token, no markup field."""
        # get_all_rates returns USD per million tokens; the router converts
        # to USD per token for the API surface.
        mock_get_rates.return_value = {
            "anthropic.claude-sonnet-4-6": {
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.3,
                "cache_write": 3.75,
            }
        }

        response = await async_client.get("/api/v1/billing/pricing")
        assert response.status_code == 200
        data = response.json()
        sonnet = data["models"]["anthropic.claude-sonnet-4-6"]
        assert sonnet["input"] == pytest.approx(3.0e-6)
        assert sonnet["output"] == pytest.approx(15.0e-6)
        assert sonnet["cache_read"] == pytest.approx(0.3e-6)
        assert sonnet["cache_write"] == pytest.approx(3.75e-6)
        assert "markup" not in data
        assert "tier_model" not in data


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

        with patch("routers.billing.BillingService") as mock_billing_cls:
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
                        "provider_choice": "bedrock_claude",
                        "clerk_user_id": "user_clerk_x",
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
        # account["owner_id"] (NOT clerk_user_id, NOT user_repo).
        mock_repo.set_provider_choice.assert_awaited_once_with(
            "org_x",
            provider_choice="bedrock_claude",
            byo_provider=None,
            owner_type="org",
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


class TestTrialCheckoutProviderRules:
    """POST /api/v1/billing/trial-checkout — org/personal provider_choice rules.

    Per memory/project_chatgpt_oauth_personal_only.md (decision 2026-04-30),
    ChatGPT OAuth is allowed only for personal/single-user workspaces. OpenAI
    Plus terms forbid reselling Plus access, so org admins cannot route their
    teammates' prompts through their personal ChatGPT subscription. Org-context
    callers picking provider_choice=chatgpt_oauth get 403; byo_key and
    bedrock_claude continue to work for orgs.
    """

    @pytest.fixture
    def override_org_admin_auth(self, app, mock_org_admin_user):
        """Swap get_current_user with an org-admin AuthContext for one test."""
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = mock_org_admin_user
        yield
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    @patch("routers.billing.billing_repo")
    @patch("core.services.billing_service.create_flat_fee_checkout")
    async def test_trial_checkout_rejects_chatgpt_oauth_for_org(
        self, mock_create_checkout, mock_repo, async_client, override_org_admin_auth
    ):
        """Org-context user calling trial-checkout with provider_choice=chatgpt_oauth gets 403.

        Mocks billing_repo + create_flat_fee_checkout so that *without* the
        org-block the request would otherwise succeed with a 200 — that way
        a green test pre-implementation is impossible (failing assert 403).
        """
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_test_456",
                "stripe_customer_id": "cus_org_chatgpt",
                "subscription_status": None,
            }
        )

        class _Session:
            url = "https://checkout.stripe.test/sess_should_not_reach"

        mock_create_checkout.return_value = _Session()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "chatgpt_oauth"},
        )
        assert resp.status_code == 403
        assert "organization" in resp.json()["detail"].lower()
        # Reject must happen BEFORE Stripe Checkout is touched — otherwise we'd
        # leak a Checkout session to a denied caller.
        mock_create_checkout.assert_not_called()

    @pytest.mark.asyncio
    @patch("routers.billing.billing_repo")
    @patch("core.services.billing_service.create_flat_fee_checkout")
    async def test_trial_checkout_allows_byo_key_for_org(
        self, mock_create_checkout, mock_repo, async_client, override_org_admin_auth
    ):
        """Org-context user can still pick byo_key — the gate is chatgpt_oauth-only."""
        # Org owner_id = org_test_456 from mock_org_admin_context.
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_test_456",
                "stripe_customer_id": "cus_org_byo",
                "subscription_status": None,
            }
        )
        # trial-checkout synchronously persists provider_choice via
        # billing_repo.set_provider_choice before creating the Stripe
        # Checkout session. Mock as AsyncMock so the await works.
        mock_repo.set_provider_choice = AsyncMock()

        class _Session:
            url = "https://checkout.stripe.test/sess_byo"

        mock_create_checkout.return_value = _Session()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "byo_key", "byo_provider": "anthropic"},
        )
        assert resp.status_code == 200
        assert resp.json()["checkout_url"] == "https://checkout.stripe.test/sess_byo"

    @pytest.mark.asyncio
    @patch("routers.billing.billing_repo")
    @patch("core.services.billing_service.create_flat_fee_checkout")
    async def test_trial_checkout_allows_bedrock_claude_for_org(
        self, mock_create_checkout, mock_repo, async_client, override_org_admin_auth
    ):
        """Org-context user can still pick bedrock_claude."""
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_test_456",
                "stripe_customer_id": "cus_org_bed",
                "subscription_status": None,
            }
        )
        # Sync-persist of provider_choice — see byo test above.
        mock_repo.set_provider_choice = AsyncMock()

        class _Session:
            url = "https://checkout.stripe.test/sess_bed"

        mock_create_checkout.return_value = _Session()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "bedrock_claude"},
        )
        assert resp.status_code == 200
        assert resp.json()["checkout_url"] == "https://checkout.stripe.test/sess_bed"

    @pytest.mark.asyncio
    @patch("routers.billing.billing_repo")
    @patch("core.services.billing_service.create_flat_fee_checkout")
    async def test_trial_checkout_allows_chatgpt_oauth_for_personal(
        self, mock_create_checkout, mock_repo, async_client
    ):
        """Personal user (no org_id) can still pick chatgpt_oauth — only orgs are blocked."""
        # Default async_client uses mock_current_user — personal mode (user_test_123, no org).
        mock_repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "stripe_customer_id": "cus_personal_chatgpt",
                "subscription_status": None,
            }
        )
        # Sync-persist of provider_choice — see byo test above.
        mock_repo.set_provider_choice = AsyncMock()

        class _Session:
            url = "https://checkout.stripe.test/sess_personal"

        mock_create_checkout.return_value = _Session()

        resp = await async_client.post(
            "/api/v1/billing/trial-checkout",
            json={"provider_choice": "chatgpt_oauth"},
        )
        assert resp.status_code == 200
        assert resp.json()["checkout_url"] == "https://checkout.stripe.test/sess_personal"
