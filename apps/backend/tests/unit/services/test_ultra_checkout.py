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


@pytest.fixture(autouse=True)
def _checkout_slot_claim_granted(monkeypatch):
    """The fresh-Ultra-checkout branch serializes session creation behind the
    same per-owner DDB claim as create_flat_fee_checkout. Grant it by default so
    the fresh-checkout tests exercise the path they were written for; the claim's
    own denial/record behavior on the Ultra path is tested explicitly below. The
    in-place-swap tests never reach the claim, so this is a harmless no-op there."""
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())


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
        # Idempotency key scoped to the current subscription item id — a retried
        # upgrade (same item) collapses to one write; a later re-upgrade on a new
        # item id gets a fresh key.
        assert kwargs["idempotency_key"] == "ultra-upgrade-owner_1-si_flat"
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
    async def test_rejects_per_seat_team_subscription(self, monkeypatch):
        """A per-seat Teams sub (subscription_item_id set) must NOT be swapped
        in place — swapping the $/seat item to the Ultra flat price would
        corrupt per-seat billing. Reject with a clear error, no Subscription
        call. (Ultra + Teams is out of scope.)"""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_seats",
                    "stripe_customer_id": "cus_org",
                    "stripe_subscription_id": "sub_seat",
                    "subscription_item_id": "si_seat",
                    "seat_count": 5,
                    "subscription_status": "active",
                }
            ),
        )
        retrieve_mock = MagicMock()
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", retrieve_mock)
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)
        builder_mock = AsyncMock()
        monkeypatch.setattr(billing_service, "_build_subscription_checkout", builder_mock)

        with pytest.raises(BillingServiceError, match="per-seat team"):
            await billing_service.create_ultra_upgrade(owner_id="org_seats")

        # No swap, no fresh checkout — pure rejection.
        retrieve_mock.assert_not_called()
        modify_mock.assert_not_called()
        builder_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_past_due_flat_sub_swaps_in_place_not_fresh_checkout(self, monkeypatch):
        """A past_due (still-billable) flat sub is a LIVE subscription — it must
        be upgraded IN PLACE, not routed to a fresh checkout (which would create
        a SECOND parallel subscription → double-bill)."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_pd",
                    "stripe_customer_id": "cus_pd",
                    "stripe_subscription_id": "sub_pd",
                    "subscription_status": "past_due",
                }
            ),
        )
        monkeypatch.setattr(
            billing_service.stripe.Subscription,
            "retrieve",
            MagicMock(return_value={"id": "sub_pd", "items": {"data": [{"id": "si_pd"}]}}),
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)
        # A fresh-checkout attempt here would be the double-bill bug — assert it
        # is never reached.
        builder_mock = AsyncMock()
        monkeypatch.setattr(billing_service, "_build_subscription_checkout", builder_mock)

        result = await billing_service.create_ultra_upgrade(owner_id="owner_pd")

        assert result == {"upgraded": True}
        modify_mock.assert_called_once()
        _, kwargs = modify_mock.call_args
        assert kwargs["items"] == [{"id": "si_pd", "price": "price_ultra_test"}]
        assert kwargs["idempotency_key"] == "ultra-upgrade-owner_pd-si_pd"
        builder_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_team_seat_sub_rejected_before_swap_even_without_local_field(self, monkeypatch):
        """F2: even when the local row hasn't persisted subscription_item_id yet
        (team-seat webhook lagged), a LIVE sub whose retrieved item is on the
        seat price must be rejected BEFORE Subscription.modify — else the swap
        would reprice a $/seat item to the Ultra flat price."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_lag",
                    "stripe_customer_id": "cus_lag",
                    "stripe_subscription_id": "sub_seat_live",
                    "subscription_status": "active",
                    # subscription_item_id intentionally ABSENT — the local guard
                    # can't fire; the LIVE inspection must catch it.
                }
            ),
        )
        retrieve_mock = MagicMock(
            return_value={
                "id": "sub_seat_live",
                "items": {"data": [{"id": "si_seat", "price": {"id": "price_seat_test"}}]},
            }
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", retrieve_mock)
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)

        with pytest.raises(BillingServiceError, match="per-seat team"):
            await billing_service.create_ultra_upgrade(owner_id="org_lag")

        retrieve_mock.assert_called_once_with("sub_seat_live")
        modify_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_sub_with_multiple_items_rejected(self, monkeypatch):
        """F2: a live sub with more than one line item is a Teams/multi-item sub
        — reject before the swap regardless of the price ids."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_multi",
                    "stripe_customer_id": "cus_multi",
                    "stripe_subscription_id": "sub_multi",
                    "subscription_status": "active",
                }
            ),
        )
        retrieve_mock = MagicMock(return_value={"id": "sub_multi", "items": {"data": [{"id": "si_1"}, {"id": "si_2"}]}})
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", retrieve_mock)
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)

        with pytest.raises(BillingServiceError, match="per-seat team"):
            await billing_service.create_ultra_upgrade(owner_id="org_multi")

        modify_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotency_key_varies_by_item_id(self, monkeypatch):
        """F3: the swap key is scoped to the current subscription item id. A
        genuine double-click (same item) collapses to one write; a later
        downgrade-then-re-upgrade (new item id) gets a fresh key so it re-swaps
        instead of returning Stripe's cached first response."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_k",
                    "stripe_customer_id": "cus_k",
                    "stripe_subscription_id": "sub_k",
                    "subscription_status": "active",
                }
            ),
        )
        modify_mock = MagicMock()
        monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify_mock)

        # First upgrade cycle: item si_A.
        monkeypatch.setattr(
            billing_service.stripe.Subscription,
            "retrieve",
            MagicMock(return_value={"id": "sub_k", "items": {"data": [{"id": "si_A"}]}}),
        )
        await billing_service.create_ultra_upgrade(owner_id="owner_k")
        key_a = modify_mock.call_args.kwargs["idempotency_key"]
        # Double-click within the SAME cycle: same item si_A → same key.
        await billing_service.create_ultra_upgrade(owner_id="owner_k")
        key_a2 = modify_mock.call_args.kwargs["idempotency_key"]

        # Later re-upgrade after a downgrade + re-subscribe: NEW item si_B.
        monkeypatch.setattr(
            billing_service.stripe.Subscription,
            "retrieve",
            MagicMock(return_value={"id": "sub_k", "items": {"data": [{"id": "si_B"}]}}),
        )
        await billing_service.create_ultra_upgrade(owner_id="owner_k")
        key_b = modify_mock.call_args.kwargs["idempotency_key"]

        assert key_a == key_a2 == "ultra-upgrade-owner_k-si_A"  # double-click de-dupes
        assert key_b == "ultra-upgrade-owner_k-si_B"  # real re-upgrade gets a fresh key
        assert key_a != key_b

    @pytest.mark.asyncio
    async def test_fresh_checkout_denied_when_claim_in_progress(self, monkeypatch):
        """F1: the fresh-Ultra-checkout branch serializes behind the same
        per-owner claim as create_flat_fee_checkout. A denied claim raises
        checkout_in_progress and never builds a session — a trial/canceled owner
        can't open duplicate concurrent Ultra checkouts."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_trial",
                    "stripe_customer_id": "cus_t",
                    "subscription_status": "trial_local",
                }
            ),
        )
        # Deny the claim (overrides the autouse grant for this test).
        monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value=False))
        builder_mock = AsyncMock()
        monkeypatch.setattr(billing_service, "_build_subscription_checkout", builder_mock)

        with pytest.raises(BillingServiceError, match="checkout_in_progress"):
            await billing_service.create_ultra_upgrade(owner_id="owner_trial")

        builder_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fresh_checkout_claims_and_records_pending_session(self, monkeypatch):
        """F1: a successful fresh Ultra checkout claims the slot and records the
        pending session (mirror create_flat_fee_checkout) so a second click
        can't mint a parallel completable session."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_trial",
                    "stripe_customer_id": "cus_t",
                    "subscription_status": "trial_local",
                }
            ),
        )
        claim_mock = AsyncMock(return_value=True)
        pending_mock = AsyncMock()
        monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", claim_mock)
        monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", pending_mock)
        monkeypatch.setattr(
            billing_service,
            "_build_subscription_checkout",
            AsyncMock(return_value=MagicMock(url="https://checkout/ultra", id="cs_ultra")),
        )

        result = await billing_service.create_ultra_upgrade(owner_id="owner_trial")

        assert result.url == "https://checkout/ultra"
        claim_mock.assert_awaited_once_with("owner_trial")
        pending_mock.assert_awaited_once()
        assert pending_mock.await_args.kwargs["session_id"] == "cs_ultra"

    @pytest.mark.asyncio
    async def test_fresh_checkout_seeds_provider_choice_into_metadata(self, monkeypatch):
        """F6: a no-sub owner with an existing provider_choice threads it into
        the fresh Ultra checkout metadata (mirror create_flat_fee_checkout) so
        they don't hit the provider_required gate after paying."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_pc",
                    "stripe_customer_id": "cus_pc",
                    "subscription_status": "trial_local",
                    "provider_choice": "bedrock_claude",
                }
            ),
        )
        seen: dict = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/ultra-pc", id="cs_pc")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)

        await billing_service.create_ultra_upgrade(owner_id="owner_pc")

        assert seen["metadata"]["provider_choice"] == "bedrock_claude"
        # Recorded pending session carries the same provider (for a later switch).
        assert seen["metadata"]["owner_id"] == "owner_pc"

    @pytest.mark.asyncio
    async def test_fresh_checkout_omits_provider_choice_when_absent(self, monkeypatch):
        """F6: an owner with NO provider_choice gets metadata WITHOUT one — the
        fix must not invent a provider."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
        monkeypatch.setattr(
            billing_service.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "owner_np",
                    "stripe_customer_id": "cus_np",
                    "subscription_status": "trial_local",
                }
            ),
        )
        seen: dict = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/ultra-np", id="cs_np")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)

        await billing_service.create_ultra_upgrade(owner_id="owner_np")

        assert "provider_choice" not in seen["metadata"]

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

    @pytest.mark.asyncio
    async def test_maps_checkout_in_progress_to_409(self, monkeypatch):
        """A concurrent fresh-Ultra checkout (claim denied → create_ultra_upgrade
        raises checkout_in_progress) surfaces as 409 Conflict, not the generic
        400 — endpoint parity with /trial-checkout so the frontend's
        already-in-progress handling works for Ultra too."""
        monkeypatch.setattr(
            billing,
            "_get_billing_account",
            AsyncMock(return_value={"owner_id": "user_p", "stripe_customer_id": "cus_p"}),
        )
        with patch(
            "core.services.billing_service.create_ultra_upgrade",
            new=AsyncMock(side_effect=BillingServiceError("checkout_in_progress")),
        ):
            with pytest.raises(HTTPException) as exc:
                await billing.create_ultra_checkout(auth=_personal_ctx())
        assert exc.value.status_code == 409
        assert exc.value.detail == "checkout_in_progress"
