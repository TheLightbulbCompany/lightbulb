"""Tests for BillingService — flat-fee model."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import billing_service
from core.services.billing_service import (
    BillingService,
    BillingServiceError,
)


class TestBillingServiceCreateCustomer:
    """Stripe customer creation with DynamoDB conditional write dedup."""

    @pytest.fixture
    def service(self):
        return BillingService()

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_for_owner_creates_when_no_email_match(self, mock_stripe, mock_repo, service):
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_stripe.Customer.list.return_value = MagicMock(data=[])
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_new_123")
        mock_repo.create_if_not_exists = AsyncMock(
            return_value={
                "owner_id": "user_new_123",
                "stripe_customer_id": "cus_new_123",
            }
        )

        account = await service.create_customer_for_owner(
            owner_id="user_new_123",
            email="test@example.com",
        )

        mock_stripe.Customer.list.assert_called_once_with(email="test@example.com", limit=1)
        mock_stripe.Customer.create.assert_called_once()
        assert account["owner_id"] == "user_new_123"
        assert account["stripe_customer_id"] == "cus_new_123"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_reuses_existing_customer_by_email(self, mock_stripe, mock_repo, service):
        """When a Stripe customer already exists for this email, reuse its id
        instead of creating a duplicate. Same human → one Stripe customer
        across personal + org billing rows."""
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_stripe.Customer.list.return_value = MagicMock(data=[MagicMock(id="cus_existing")])
        mock_repo.create_if_not_exists = AsyncMock(
            return_value={
                "owner_id": "org_456",
                "stripe_customer_id": "cus_existing",
                "owner_type": "org",
            }
        )

        account = await service.create_customer_for_owner(
            owner_id="org_456",
            owner_type="org",
            email="admin@example.com",
        )

        mock_stripe.Customer.list.assert_called_once_with(email="admin@example.com", limit=1)
        mock_stripe.Customer.create.assert_not_called()
        assert account["stripe_customer_id"] == "cus_existing"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_race_returns_winner_no_orphan_delete(self, mock_stripe, mock_repo, service):
        """If two callers race on the same email, both find the same Stripe
        customer (email-keyed) — no orphan to delete."""
        from core.repositories.billing_repo import AlreadyExistsError

        mock_repo.AlreadyExistsError = AlreadyExistsError
        mock_repo.get_by_owner_id = AsyncMock(
            side_effect=[
                None,
                {"owner_id": "user_race", "stripe_customer_id": "cus_shared"},
            ]
        )
        mock_stripe.Customer.list.return_value = MagicMock(data=[MagicMock(id="cus_shared")])
        mock_repo.create_if_not_exists = AsyncMock(side_effect=AlreadyExistsError("user_race"))

        result = await service.create_customer_for_owner(
            owner_id="user_race",
            email="race@example.com",
        )

        mock_stripe.Customer.create.assert_not_called()
        mock_stripe.Customer.delete.assert_not_called()
        assert result["stripe_customer_id"] == "cus_shared"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_race_on_create_path_deletes_orphan(self, mock_stripe, mock_repo, service):
        """If two callers race on the create-path (both miss the email
        lookup, e.g. brand-new email), each mints a separate Stripe
        customer. Only one DDB write wins — the loser's customer must be
        deleted so the email→customer invariant holds."""
        from core.repositories.billing_repo import AlreadyExistsError

        mock_repo.AlreadyExistsError = AlreadyExistsError
        mock_repo.get_by_owner_id = AsyncMock(
            side_effect=[
                None,
                {"owner_id": "user_race", "stripe_customer_id": "cus_winner"},
            ]
        )
        mock_stripe.Customer.list.return_value = MagicMock(data=[])
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_loser")
        mock_repo.create_if_not_exists = AsyncMock(side_effect=AlreadyExistsError("user_race"))

        result = await service.create_customer_for_owner(
            owner_id="user_race",
            email="race@example.com",
        )

        mock_stripe.Customer.create.assert_called_once()
        mock_stripe.Customer.delete.assert_called_once()
        delete_args, delete_kwargs = mock_stripe.Customer.delete.call_args
        assert delete_args[0] == "cus_loser"
        assert delete_kwargs.get("idempotency_key") == "delete_customer:cus_loser"
        assert result["stripe_customer_id"] == "cus_winner"

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_normalizes_email_case_and_whitespace(self, mock_stripe, mock_repo, service):
        """``Customer.list(email=...)`` is exact-match, so we normalize
        email (strip + lowercase) before any Stripe call. Without this, a
        single human signing in as ``User@Example.com`` once and
        ``user@example.com`` later would create two Stripe customers."""
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_stripe.Customer.list.return_value = MagicMock(data=[MagicMock(id="cus_norm")])
        mock_repo.create_if_not_exists = AsyncMock(
            return_value={"owner_id": "u_norm", "stripe_customer_id": "cus_norm"}
        )

        await service.create_customer_for_owner(
            owner_id="u_norm",
            email="  User@Example.COM  ",
        )

        mock_stripe.Customer.list.assert_called_once_with(email="user@example.com", limit=1)
        mock_stripe.Customer.create.assert_not_called()

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_idempotent(self, mock_stripe, mock_repo, service):
        existing = {
            "owner_id": "user_idem",
            "stripe_customer_id": "cus_idem",
            "id": "acc-123",
        }
        mock_repo.get_by_owner_id = AsyncMock(return_value=existing)

        result = await service.create_customer_for_owner(
            owner_id="user_idem",
            email="idem@example.com",
        )

        assert result["id"] == "acc-123"
        mock_stripe.Customer.list.assert_not_called()
        mock_stripe.Customer.create.assert_not_called()


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


class TestCreateFlatFeeCheckout:
    """Tests for the flat-fee Checkout helper used by the post-pivot onboarding
    wizard (single $50/mo price, no per-tier branching)."""

    @pytest.mark.asyncio
    async def test_create_flat_fee_checkout_uses_flat_price_and_tax(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        fake_session = MagicMock(url="https://checkout/x", id="cs_test")
        with (
            patch.object(billing_service.stripe.checkout.Session, "create", return_value=fake_session) as mock_create,
            patch.object(
                billing_service.billing_repo,
                "get_by_owner_id",
                new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
            ),
        ):
            result = await billing_service.create_flat_fee_checkout(owner_id="u_1")

        assert result is fake_session
        _, kwargs = mock_create.call_args
        assert kwargs["line_items"] == [{"price": "price_flat_test", "quantity": 1}]
        assert kwargs["automatic_tax"] == {"enabled": True}
        assert kwargs["customer_update"] == {"address": "auto"}
        assert kwargs["allow_promotion_codes"] is True
        assert kwargs["customer"] == "cus_test"
        assert kwargs["mode"] == "subscription"
        # 5-minute idempotency bucket collapses rapid duplicate clicks and
        # multi-tab retries to a single Checkout Session, preventing
        # parallel subscriptions before the webhook + guard catches up.
        assert kwargs["idempotency_key"].startswith("flat_checkout:u_1:")
        # owner_id threaded into subscription metadata so the webhook can
        # resolve owner unambiguously when one Stripe customer is shared
        # across multiple billing rows.
        assert kwargs["subscription_data"]["metadata"]["owner_id"] == "u_1"

    @pytest.mark.asyncio
    async def test_create_flat_fee_checkout_raises_without_flat_price_id(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_FLAT_PRICE_ID not configured"):
            await billing_service.create_flat_fee_checkout(owner_id="u_1")

    @pytest.mark.asyncio
    async def test_create_flat_fee_checkout_raises_without_stripe_customer(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(BillingServiceError, match="No Stripe customer"):
                await billing_service.create_flat_fee_checkout(owner_id="u_missing")


@pytest.mark.asyncio
async def test_change_provider_into_bedrock_takes_cold_path(monkeypatch):
    """A switch INTO bedrock_claude must NOT take the hot path (which would
    call build_provider_block(bedrock, aip_arns=None) and raise), and must
    NOT raise — it dispatches background_provision (cold path)."""
    # No prior provider_choice -> none of the OLD-provider side-effect cleanup
    # blocks (oauth, byo) fire, keeping the test free of oauth/key I/O while
    # still exercising the bedrock hot-vs-cold selector.
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
            }
        ),
    )
    # change_provider imports is_subscription_active LAZILY from
    # core.services.provision_gate, so patch it at the source module.
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    hot = AsyncMock()
    monkeypatch.setattr(billing_service, "_hot_swap_provider", hot)
    bg = AsyncMock()
    monkeypatch.setattr("core.services.container_provision.background_provision", bg)
    created = []
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: created.append(coro) or MagicMock(),
    )

    result = await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    hot.assert_not_awaited()  # hot path skipped for bedrock target
    assert result["restart"] is True
    # background_provision (cold path) was scheduled.
    assert created, "expected background_provision to be dispatched"
    for coro in created:
        coro.close()


@pytest.mark.asyncio
async def test_hot_swap_pre_stages_under_owner_id(monkeypatch):
    """One ChatGPT account ↔ one container: the token READ is keyed on the
    switcher (the connector), but the EFS auth file is WRITTEN under the
    container OWNER (org_id for orgs). For an org the two ids differ — pin
    that the read uses clerk_user_id and the write uses owner_id."""
    get_tokens = AsyncMock(return_value={"access_token": "tok", "account_id": "acc"})
    monkeypatch.setattr("core.services.oauth_service.get_decrypted_tokens", get_tokens)
    pre_stage = AsyncMock()
    monkeypatch.setattr("core.containers.workspace.pre_stage_auth_profile_store", pre_stage)
    # Stub the config side so the helper reaches the patch step without real
    # config I/O. build_provider_block returns the (providers, default, plugins)
    # triple; replace_provider_block is the EFS write we don't care about here.
    monkeypatch.setattr(
        "core.containers.config.build_provider_block",
        lambda **kwargs: ({}, {"primary": "p", "fallbacks": ["p"]}, {}),
    )
    monkeypatch.setattr("core.containers.config.premium_model_ref", lambda *a, **k: None)
    monkeypatch.setattr("core.containers.config.cheap_model_ref", lambda *a, **k: "cheap")
    monkeypatch.setattr("core.services.config_patcher.replace_provider_block", AsyncMock())

    await billing_service._hot_swap_provider(
        owner_id="org_1",
        clerk_user_id="user_a",
        new_provider="chatgpt_oauth",
    )

    # Token read keyed on the human connector (the switcher).
    get_tokens.assert_awaited_once_with(user_id="user_a")
    # Auth file written under the container owner (org_1), not the connector.
    pre_stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "tok", "account_id": "acc"})
