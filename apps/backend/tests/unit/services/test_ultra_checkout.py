"""Tests for the Ultra Pro upgrade — create_ultra_upgrade service + endpoint.

An owner on the $50 flat subscription upgrades to Ultra Pro ($150) by swapping
the subscription item's price to the Ultra price IN PLACE (one subscription,
prorated). An owner with no live subscription (trial_local / never subscribed)
falls back to a fresh Ultra Checkout session. The durable tier write + resize
happen in the webhook (Task 4), never here.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from core.services import billing_service  # noqa: E402
from core.services.billing_service import BillingServiceError  # noqa: E402
from routers import billing  # noqa: E402


# ---------------------------------------------------------------------------
# Service: create_ultra_upgrade
# ---------------------------------------------------------------------------


class TestCreateUltraUpgradeService:
    @pytest.mark.asyncio
    async def test_active_flat_sub_swaps_item_price_in_place(self, monkeypatch):
        """An owner on a live flat sub is upgraded IN PLACE: retrieve the sub,
        read the item id LIVE off it, and modify that item's price to Ultra
        with a stable idempotency key + proration. Returns {"upgraded": True};
        no fresh checkout session is created."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_1",
                    "stripe_customer_id": "cus_1",
                    "stripe_subscription_id": "sub_flat",
                    "subscription_status": "active",
                }
            ),
        )
        # The flat sub carries a single item; its id is NOT persisted on the
        # billing row, so it is read live off the retrieved subscription.
        fake_sub = {"id": "sub_flat", "items": {"data": [{"id": "si_flat"}]}}
        retrieve_mock = MagicMock(return_value=fake_sub)
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", retrieve_mock)
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)
        # The fresh-checkout builder must NOT be reached on the in-place path.
        builder_mock = AsyncMock()
        monkeypatch.setattr(billing_service, "_build_subscription_checkout", builder_mock)

        result = await billing_service.create_ultra_upgrade(owner_id="owner_1")

        assert result == {"upgraded": True}
        retrieve_mock.assert_called_once_with("sub_flat")
        modify_mock.assert_called_once()
        args, kwargs = modify_mock.call_args
        assert args[0] == "sub_flat"
        # The Ultra price swapped onto the EXISTING item id (read live).
        assert kwargs["items"] == [{"id": "si_flat", "price": "price_ultra_test"}]
        assert kwargs["proration_behavior"] == "create_prorations"
        # Stable idempotency key — a retried upgrade collapses to one write.
        assert kwargs["idempotency_key"] == "ultra-upgrade-owner_1"
        builder_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trialing_flat_sub_also_swaps_in_place(self, monkeypatch):
        """A `trialing` (legacy Stripe-trial) flat sub with a real sub id is
        also a live subscription and upgrades in place."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_tr",
                    "stripe_subscription_id": "sub_tr",
                    "subscription_status": "trialing",
                }
            ),
        )
        monkeypatch.setattr(
            billing_service.stripe.Subscription,
            "retrieve",
            MagicMock(return_value={"id": "sub_tr", "items": {"data": [{"id": "si_tr"}]}}),
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)

        result = await billing_service.create_ultra_upgrade(owner_id="owner_tr")

        assert result == {"upgraded": True}
        modify_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_sub_builds_fresh_ultra_checkout(self, monkeypatch):
        """A trial_local owner (no live Stripe subscription to swap) gets a
        fresh Ultra Checkout on the Ultra price, quantity 1, purpose=ultra —
        and no Subscription.modify is attempted."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_trial",
                    "stripe_customer_id": "cus_trial",
                    "subscription_status": "trial_local",
                }
            ),
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)

        seen: dict = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/ultra", id="cs_ultra")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)

        result = await billing_service.create_ultra_upgrade(owner_id="owner_trial")

        assert result.url == "https://checkout/ultra"
        assert seen["price_id"] == "price_ultra_test"
        assert seen["quantity"] == 1
        # purpose=ultra rides the subscription metadata so the webhook records
        # the Ultra tier when the subscription first goes live.
        assert seen["metadata"] == {"owner_id": "owner_trial", "purpose": "ultra"}
        assert seen["idempotency_key"].startswith("ultra_checkout:owner_trial:")
        modify_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_subscribed_builds_fresh_checkout(self, monkeypatch):
        """An owner with no billing row at all (never subscribed) also takes
        the fresh-checkout branch."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))

        seen: dict = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/ultra-new")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)

        result = await billing_service.create_ultra_upgrade(owner_id="owner_new", clerk_user_id="user_new")

        assert result.url == "https://checkout/ultra-new"
        assert seen["price_id"] == "price_ultra_test"
        # clerk_user_id threaded into metadata (mirrors the flat checkout) so
        # the webhook can resolve the per-Clerk-user row.
        assert seen["metadata"] == {"owner_id": "owner_new", "purpose": "ultra", "clerk_user_id": "user_new"}

    @pytest.mark.asyncio
    async def test_canceled_row_with_stale_sub_id_builds_fresh_checkout(self, monkeypatch):
        """A non-live status (canceled) must NOT be swapped in place even if a
        stale stripe_subscription_id lingers on the row — it routes to a fresh
        checkout instead of modifying a dead subscription."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_canceled",
                    "stripe_customer_id": "cus_c",
                    "stripe_subscription_id": "sub_dead",
                    "subscription_status": "canceled",
                }
            ),
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)
        monkeypatch.setattr(
            billing_service,
            "_build_subscription_checkout",
            AsyncMock(return_value=MagicMock(url="https://checkout/ultra-c")),
        )

        result = await billing_service.create_ultra_upgrade(owner_id="owner_canceled")

        assert result.url == "https://checkout/ultra-c"
        modify_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_without_ultra_price_id(self, monkeypatch):
        """An unconfigured Ultra price fails clearly (mirrors the flat / team
        price-id guards) before any Stripe call."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_ULTRA_PRICE_ID not configured"):
            await billing_service.create_ultra_upgrade(owner_id="owner_1")


# ---------------------------------------------------------------------------
# Endpoint: POST /billing/ultra-checkout
# ---------------------------------------------------------------------------


def _personal_ctx():
    return AuthContext(user_id="user_p", email="p@example.com")


def _admin_ctx():
    return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin", email="admin@example.com")


def _member_ctx():
    return AuthContext(user_id="user_mem", org_id="org_1", org_role="org:member", email="mem@example.com")


class TestUltraCheckoutEndpoint:
    @pytest.mark.asyncio
    async def test_returns_upgraded_for_in_place_swap(self, monkeypatch):
        """The in-place swap result {"upgraded": True} is returned verbatim —
        no checkout_url."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "user_p", "stripe_customer_id": "cus_p"}),
        )
        upgrade_mock = AsyncMock(return_value={"upgraded": True})
        with patch("core.services.billing_service.create_ultra_upgrade", new=upgrade_mock):
            result = await billing.create_ultra_checkout(auth=_personal_ctx())

        assert result == {"upgraded": True}
        # Owner-scoped: called with the resolved owner id (user id for personal).
        upgrade_mock.assert_awaited_once_with(owner_id="user_p", clerk_user_id="user_p")

    @pytest.mark.asyncio
    async def test_returns_checkout_url_for_fresh_checkout(self, monkeypatch):
        """A returned Checkout Session maps to {"checkout_url": session.url}."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "user_p", "stripe_customer_id": "cus_p"}),
        )
        fake_session = MagicMock(url="https://checkout/ultra")
        with patch("core.services.billing_service.create_ultra_upgrade", new=AsyncMock(return_value=fake_session)):
            result = await billing.create_ultra_checkout(auth=_personal_ctx())

        assert result == {"checkout_url": "https://checkout/ultra"}

    @pytest.mark.asyncio
    async def test_owner_scoped_to_org_id_for_org_admin(self, monkeypatch):
        """In org context the upgrade keys on the org id, not the calling
        admin's user id."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_org"}),
        )
        upgrade_mock = AsyncMock(return_value={"upgraded": True})
        with patch("core.services.billing_service.create_ultra_upgrade", new=upgrade_mock):
            await billing.create_ultra_checkout(auth=_admin_ctx())

        upgrade_mock.assert_awaited_once_with(owner_id="org_1", clerk_user_id="user_admin")

    @pytest.mark.asyncio
    async def test_rejects_non_admin_org_member(self, monkeypatch):
        """A non-admin org member cannot bump the org onto the Ultra tier."""
        with pytest.raises(HTTPException) as exc:
            await billing.create_ultra_checkout(auth=_member_ctx())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_ensures_stripe_customer_before_fresh_checkout(self, monkeypatch):
        """A trial_local owner carries no stripe_customer_id — the endpoint
        backfills one (mirrors /trial-checkout, /team-checkout) before the
        fresh Ultra checkout can build."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "user_p", "subscription_status": "trial_local"}),
        )
        create_customer = AsyncMock(return_value={"owner_id": "user_p", "stripe_customer_id": "cus_new"})
        mock_bs = MagicMock()
        mock_bs.create_customer_for_owner = create_customer
        monkeypatch.setattr(billing, "BillingService", MagicMock(return_value=mock_bs))

        fake_session = MagicMock(url="https://checkout/ultra-fresh")
        with patch("core.services.billing_service.create_ultra_upgrade", new=AsyncMock(return_value=fake_session)):
            result = await billing.create_ultra_checkout(auth=_personal_ctx())

        create_customer.assert_awaited_once()
        assert result == {"checkout_url": "https://checkout/ultra-fresh"}

    @pytest.mark.asyncio
    async def test_maps_billing_service_error_to_400(self, monkeypatch):
        """An unconfigured Ultra price (BillingServiceError) surfaces as 400."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "user_p", "stripe_customer_id": "cus_p"}),
        )
        with patch(
            "core.services.billing_service.create_ultra_upgrade",
            new=AsyncMock(side_effect=BillingServiceError("STRIPE_ULTRA_PRICE_ID not configured")),
        ):
            with pytest.raises(HTTPException) as exc:
                await billing.create_ultra_checkout(auth=_personal_ctx())
        assert exc.value.status_code == 400
