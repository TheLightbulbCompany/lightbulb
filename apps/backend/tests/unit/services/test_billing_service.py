"""Tests for BillingService — flat-fee model."""

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.services import billing_service
from core.services.billing_service import (
    BillingService,
    BillingServiceError,
)


@pytest.fixture(autouse=True)
def _checkout_slot_claim_granted():
    """create_flat_fee_checkout serializes session creation behind a
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
        mock_repo.set_stripe_customer_id = AsyncMock()

        result = await service.create_customer_for_owner(
            owner_id="user_idem",
            email="idem@example.com",
        )

        assert result["id"] == "acc-123"
        mock_stripe.Customer.list.assert_not_called()
        mock_stripe.Customer.create.assert_not_called()
        # A row that already has a stripe_customer_id must never be backfilled
        # or re-created — the short-circuit stays byte-identical to today.
        mock_repo.set_stripe_customer_id.assert_not_called()
        mock_repo.create_if_not_exists.assert_not_called()

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_backfills_trial_local_row_missing_stripe_customer(
        self, mock_stripe, mock_repo, service
    ):
        """LIVE BUG regression: a trial_local billing row exists (written by
        create_trial_local_if_absent, which deliberately writes no stripe_*
        fields) but has no stripe_customer_id. The old
        `if existing: return existing` short-circuit returned this row as-is,
        so create_flat_fee_checkout always raised "No Stripe customer" and
        every free-tier Upgrade click 400'd.

        create_customer_for_owner must instead mint/reuse a Stripe customer,
        backfill it onto the EXISTING row via set_stripe_customer_id (never
        create_if_not_exists, which would raise AlreadyExistsError against a
        row that's already there), and return the updated row."""
        trial_row = {
            "owner_id": "user_trial",
            "owner_type": "personal",
            "subscription_status": "trial_local",
            "trial_end": 1234567890,
        }
        mock_repo.get_by_owner_id = AsyncMock(return_value=trial_row)
        mock_repo.set_stripe_customer_id = AsyncMock()
        mock_repo.create_if_not_exists = AsyncMock()
        mock_stripe.Customer.list.return_value = MagicMock(data=[])
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_backfilled")

        account = await service.create_customer_for_owner(
            owner_id="user_trial",
            owner_type="personal",
            email="trial@example.com",
        )

        mock_stripe.Customer.create.assert_called_once()
        mock_repo.set_stripe_customer_id.assert_awaited_once_with("user_trial", stripe_customer_id="cus_backfilled")
        # Never attempted a fresh row create — the row already existed.
        mock_repo.create_if_not_exists.assert_not_called()
        assert account["stripe_customer_id"] == "cus_backfilled"
        # Rest of the trial row's state (subscription_status, trial_end) is
        # preserved, not clobbered.
        assert account["subscription_status"] == "trial_local"
        assert account["trial_end"] == 1234567890

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_backfill_reuses_existing_stripe_customer_by_email(
        self, mock_stripe, mock_repo, service
    ):
        """The backfill path still goes through the email-dedup lookup first
        — a trial_local owner whose email already has a Stripe customer (e.g.
        from a previous org) must reuse it, not mint a duplicate."""
        trial_row = {
            "owner_id": "org_trial",
            "owner_type": "org",
            "subscription_status": "trial_local",
        }
        mock_repo.get_by_owner_id = AsyncMock(return_value=trial_row)
        mock_repo.set_stripe_customer_id = AsyncMock()
        mock_stripe.Customer.list.return_value = MagicMock(data=[MagicMock(id="cus_existing_email")])

        account = await service.create_customer_for_owner(
            owner_id="org_trial",
            owner_type="org",
            email="reuse@example.com",
        )

        mock_stripe.Customer.create.assert_not_called()
        mock_repo.set_stripe_customer_id.assert_awaited_once_with("org_trial", stripe_customer_id="cus_existing_email")
        assert account["stripe_customer_id"] == "cus_existing_email"

    @pytest.mark.asyncio
    async def test_trial_local_owner_reaches_checkout_after_stripe_customer_backfill(self, monkeypatch):
        """End-to-end regression test for the live bug: a trial_local row
        (row exists, no stripe_customer_id) must be able to reach checkout —
        create_customer_for_owner backfills a Stripe customer onto the row,
        and create_flat_fee_checkout (re-reading the now-updated row)
        succeeds instead of raising "No Stripe customer for owner_id=...".
        """
        trial_row = {
            "owner_id": "user_trial_e2e",
            "owner_type": "personal",
            "subscription_status": "trial_local",
            "trial_end": 1234567890,
        }
        state = dict(trial_row)

        async def fake_get_by_owner_id(owner_id):
            assert owner_id == "user_trial_e2e"
            return dict(state)

        set_customer_id_calls = []

        async def fake_set_stripe_customer_id(owner_id, *, stripe_customer_id):
            set_customer_id_calls.append((owner_id, stripe_customer_id))
            state["stripe_customer_id"] = stripe_customer_id

        create_if_not_exists_calls = []

        async def fake_create_if_not_exists(**kwargs):
            create_if_not_exists_calls.append(kwargs)
            raise AssertionError("create_if_not_exists must not be called for an existing row")

        monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", fake_get_by_owner_id)
        monkeypatch.setattr(billing_service.billing_repo, "set_stripe_customer_id", fake_set_stripe_customer_id)
        monkeypatch.setattr(billing_service.billing_repo, "create_if_not_exists", fake_create_if_not_exists)
        monkeypatch.setattr(billing_service.stripe.Customer, "list", MagicMock(return_value=MagicMock(data=[])))
        monkeypatch.setattr(
            billing_service.stripe.Customer,
            "create",
            MagicMock(return_value=MagicMock(id="cus_e2e_backfilled")),
        )

        account = await BillingService().create_customer_for_owner(
            owner_id="user_trial_e2e",
            owner_type="personal",
            email="trial-e2e@example.com",
        )

        assert account["stripe_customer_id"] == "cus_e2e_backfilled"
        assert set_customer_id_calls == [("user_trial_e2e", "cus_e2e_backfilled")]
        assert create_if_not_exists_calls == []

        # Now the actual checkout path: it re-reads the billing row via
        # billing_repo.get_by_owner_id, which now returns the backfilled
        # stripe_customer_id — so it must succeed, not raise BillingServiceError.
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        fake_session = MagicMock(url="https://checkout/trial", id="cs_trial")
        monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", MagicMock(return_value=fake_session))

        session = await billing_service.create_flat_fee_checkout(
            owner_id="user_trial_e2e", provider_choice="bedrock_claude"
        )

        assert session is fake_session

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    @patch("core.services.billing_service.stripe")
    async def test_create_customer_for_owner_creates_new_row_when_none_exists(self, mock_stripe, mock_repo, service):
        """An owner with no billing row at all keeps today's create path —
        set_stripe_customer_id (the backfill setter) must never be reached."""
        mock_repo.get_by_owner_id = AsyncMock(return_value=None)
        mock_repo.set_stripe_customer_id = AsyncMock()
        mock_stripe.Customer.list.return_value = MagicMock(data=[])
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_fresh")
        mock_repo.create_if_not_exists = AsyncMock(
            return_value={"owner_id": "user_fresh", "stripe_customer_id": "cus_fresh"}
        )

        account = await service.create_customer_for_owner(owner_id="user_fresh", email="fresh@example.com")

        mock_repo.create_if_not_exists.assert_called_once()
        mock_repo.set_stripe_customer_id.assert_not_called()
        assert account["stripe_customer_id"] == "cus_fresh"


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
            clear_seat_fields=True,
        )

    @pytest.mark.asyncio
    async def test_cancel_subscription_clears_stale_seat_fields(self, dynamodb_billing_table):
        """Regression: cancelling an org's per-seat subscription must clear
        subscription_item_id + seat_count on the billing row, not just the
        subscription id/status. A stale subscription_item_id left behind
        makes ``is_seat_subscription`` (routers/billing.py) report True for
        an org that is no longer on a per-seat Stripe subscription --
        re-enabling fabricated "N x $20" seat pricing after e.g. the org is
        re-comped onto a flat price (whose webhook never touches
        subscription_item_id)."""
        from core.repositories import billing_repo

        await billing_repo.create_if_not_exists("org_stale_seats", "cus_stale", owner_type="org")
        await billing_repo.set_team_subscription(
            "org_stale_seats",
            subscription_id="sub_seat_1",
            subscription_item_id="si_seat_1",
            status="active",
            seat_count=7,
        )
        billing_row = await billing_repo.get_by_owner_id("org_stale_seats")
        assert billing_row["subscription_item_id"] == "si_seat_1"
        assert billing_row["seat_count"] == 7

        await BillingService().cancel_subscription(billing_row)

        after = await billing_repo.get_by_owner_id("org_stale_seats")
        assert after["subscription_status"] == "canceled"
        assert after["stripe_subscription_id"] is None
        assert after.get("subscription_item_id") is None
        assert after.get("seat_count") is None
        # Mirrors routers/billing.py's is_seat_subscription predicate.
        assert bool(after.get("subscription_item_id")) is False


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
        assert kwargs["idempotency_key"].startswith("flat_checkout:v")
        # owner_id threaded into subscription metadata so the webhook can
        # resolve owner unambiguously when one Stripe customer is shared
        # across multiple billing rows.
        assert kwargs["subscription_data"]["metadata"]["owner_id"] == "u_1"
        # Billed immediately — no Stripe trial. No-card evaluation happens
        # on the trial_local free tier, never at checkout.
        assert "trial_period_days" not in kwargs["subscription_data"]
        # Short session expiry (Stripe minimum is 30 min) bounds how long an
        # abandoned session stays completable — load-bearing for the
        # single-open-session guard in /trial-checkout.
        import time as _time

        assert 30 * 60 <= kwargs["expires_at"] - int(_time.time()) <= 32 * 60

    @pytest.mark.asyncio
    async def test_create_flat_fee_checkout_records_pending_session(self, monkeypatch):
        """The pending-checkout marker is the single-open-session guard's
        data source — every session create must record it."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        fake_session = MagicMock(url="https://checkout/x", id="cs_recorded")
        with (
            patch.object(billing_service.stripe.checkout.Session, "create", return_value=fake_session),
            patch.object(
                billing_service.billing_repo,
                "get_by_owner_id",
                new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
            ),
            patch.object(
                billing_service.billing_repo,
                "set_pending_checkout",
                new=AsyncMock(),
            ) as mock_mark,
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1", provider_choice="chatgpt_oauth")

        mock_mark.assert_awaited_once_with("u_1", session_id="cs_recorded", provider_choice="chatgpt_oauth")

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


class TestBuildSubscriptionCheckoutSharedSpine:
    """The flat and team paths must both go through _build_subscription_checkout
    so the session kwargs / tax block / idempotency shape exist in exactly one
    place (DRY spine, design §DRY spine item 1)."""

    @pytest.mark.asyncio
    async def test_flat_checkout_delegates_to_shared_builder(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            result = await billing_service.create_flat_fee_checkout(
                owner_id="u_1", provider_choice="bedrock_claude", credits_cents=2000
            )

        assert result.url == "https://checkout/flat"
        # bedrock_claude carries the starter credits as a SECOND, one-time line
        # item so the owner pays $70 once instead of $50 now and a surprise $20
        # at CreditsStep — after the container is already bought and unable to
        # boot (provision_gate Layer 3 refuses to start on a zero balance).
        assert seen["line_items"] == [
            {"price": "price_flat_test", "quantity": 1},
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": billing_service.BEDROCK_STARTER_CREDITS_CENTS,
                    "product_data": {
                        "name": "Claude credits",
                        "description": "Prepaid Claude inference balance.",
                    },
                },
                "quantity": 1,
            },
        ]
        assert seen["metadata"]["owner_id"] == "u_1"
        assert seen["metadata"]["provider_choice"] == "bedrock_claude"
        # The webhook grants off metadata, not off the line items, so a
        # 100%-off promo code still funds the balance.
        assert seen["metadata"]["credits_cents"] == str(billing_service.BEDROCK_STARTER_CREDITS_CENTS)
        assert seen["idempotency_key"].startswith("flat_checkout:v")

    @pytest.mark.asyncio
    async def test_bedrock_without_an_explicit_amount_bundles_no_credits(self, monkeypatch):
        """Absent credits_cents means NO credits line item, never a default.

        The amount is quoted to the customer before they pay, and only a client
        that renders the chooser knows what was quoted. A cached bundle, the
        mobile app, or anything predating this change posts provider_choice
        alone — defaulting to $20 there would charge $70 against a UI showing
        $50. Those callers keep the old behaviour: $50 now, credits later.
        """
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1", provider_choice="bedrock_claude")

        assert seen["line_items"] == [{"price": "price_flat_test", "quantity": 1}]
        assert "credits_cents" not in seen["metadata"]
        # Promotion codes stay available when no credits ride along.
        assert seen["allow_promotion_codes"] is True

    @pytest.mark.asyncio
    async def test_promotion_codes_are_off_when_credits_are_bundled(self, monkeypatch):
        """A subscription-intended coupon must not discount prepaid credits.

        The webhook grants the full metadata amount, so a percentage-off code
        applied across the session would hand over up to $1,000 of balance for
        less (or none) of the money. The standalone top-up can allow coupons
        because that session contains only credits the owner chose.
        """
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            await billing_service.create_flat_fee_checkout(
                owner_id="u_1", provider_choice="bedrock_claude", credits_cents=100_000
            )

        assert seen["allow_promotion_codes"] is False

    def test_normalize_starter_credits_keeps_absent_distinct_from_zero(self):
        """`or` treated a deliberate 0 as "not supplied" and substituted $20 —
        money nobody asked for, and the clamp below it never saw the zero."""
        assert billing_service.normalize_starter_credits(None) is None
        assert billing_service.normalize_starter_credits(0) == billing_service.MIN_STARTER_CREDITS_CENTS
        assert billing_service.normalize_starter_credits(2000) == 2000
        assert billing_service.normalize_starter_credits(10**9) == billing_service.MAX_STARTER_CREDITS_CENTS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("choice", ["chatgpt_oauth", "byo_key"])
    async def test_flat_checkout_bundles_credits_only_for_bedrock(self, monkeypatch, choice):
        """ChatGPT / BYO owners bring their own inference — charging them for
        Claude credits they can never spend would be taking money for nothing.
        Asserting the ABSENCE is the point: the credits line item is priced
        inline, so a mis-scoped conditional silently overcharges by $20 with
        no config change to notice."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1", provider_choice=choice)

        assert seen["line_items"] == [{"price": "price_flat_test", "quantity": 1}]
        assert "credits_cents" not in seen["metadata"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "requested,expected",
        [
            (500, 500),  # the $5 floor is a legal choice, not a rejection
            (10_000, 10_000),  # $100 preset
            (1, billing_service.MIN_STARTER_CREDITS_CENTS),  # clamped up
            (99_999_999, billing_service.MAX_STARTER_CREDITS_CENTS),  # clamped down
        ],
    )
    async def test_starter_credits_amount_is_chosen_and_clamped(self, monkeypatch, requested, expected):
        """The owner picks the starter amount; this function is the backstop.

        Pydantic bounds the value at the router, but create_flat_fee_checkout is
        importable — a caller that skips the router must not be able to mint a
        $0 line item (Stripe rejects it, checkout dies) or a $1M one.
        """
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            await billing_service.create_flat_fee_checkout(
                owner_id="u_1", provider_choice="bedrock_claude", credits_cents=requested
            )

        assert seen["line_items"][1]["price_data"]["unit_amount"] == expected
        # The line item and the metadata the webhook grants from must agree —
        # if they diverge the customer is charged one amount and credited
        # another.
        assert seen["metadata"]["credits_cents"] == str(expected)

    @pytest.mark.asyncio
    async def test_idempotency_key_varies_with_the_credit_amount(self, monkeypatch):
        """The key is bucketed to 5 minutes to collapse double-clicks. Without
        the amount in it, an owner who picks $20, backs out, and picks $100
        inside that window reuses one key with two different bodies — Stripe
        rejects the mismatch and the larger purchase just fails."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        keys = []

        async def _fake_builder(**kwargs):
            keys.append(kwargs["idempotency_key"])
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            for cents in (2000, 10_000):
                await billing_service.create_flat_fee_checkout(
                    owner_id="u_1", provider_choice="bedrock_claude", credits_cents=cents
                )

        assert keys[0] != keys[1]

    @pytest.mark.asyncio
    async def test_credits_metadata_lands_on_the_SESSION_not_only_the_subscription(self, monkeypatch):
        """Mocks the real Stripe SDK boundary, not our own builder.

        Stripe puts `subscription_data.metadata` on the created SUBSCRIPTION and
        top-level `metadata` on the SESSION, and they surface in different
        webhooks: customer.subscription.* carries the former,
        checkout.session.completed carries the latter. handle_checkout_session
        reads the SESSION's metadata, so credits bundled into a subscription
        Checkout are granted only if `credits_cents` is on the top-level dict.

        The builder previously set subscription_data alone. Every other test
        here monkeypatches _build_subscription_checkout and asserts on the
        metadata passed INTO it, so all of them passed while the real call
        omitted the field — the customer was charged for the credits line item
        and the balance was never funded. This test exists at the SDK boundary
        precisely because that is the only place the bug was visible.
        """
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", _fake_create)
        with patch.object(
            billing_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            await billing_service.create_flat_fee_checkout(
                owner_id="u_1", provider_choice="bedrock_claude", credits_cents=5000
            )

        # What checkout.session.completed will actually carry.
        assert captured["metadata"]["credits_cents"] == "5000"
        assert captured["metadata"]["owner_id"] == "u_1"
        # And the subscription copy stays, for customer.subscription.*.
        assert captured["subscription_data"]["metadata"]["provider_choice"] == "bedrock_claude"

    @pytest.mark.asyncio
    async def test_team_checkout_uses_seat_price_and_quantity(self, monkeypatch):
        """Team checkout emits [base x1, seat xN] — a solo admin is $50 + 1x$20."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "price_base_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team", id="cs_team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        result = await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

        assert result.url == "https://checkout/team"
        assert seen["line_items"] == [
            {"price": "price_base_test", "quantity": 1},
            {"price": "price_seat_test", "quantity": 4},
        ]
        # provider_choice threaded in so the webhook backup persists teams as
        # Bedrock-managed (there is no provider picker in the team flow).
        assert seen["metadata"] == {
            "owner_id": "org_1",
            "purpose": "team_seat",
            "provider_choice": "bedrock_claude",
        }
        assert seen["idempotency_key"].startswith("team_checkout:v")

    @pytest.mark.asyncio
    async def test_team_checkout_raises_without_seat_price_id(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_TEAM_SEAT_PRICE_ID not configured"):
            await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

    @pytest.mark.asyncio
    async def test_team_checkout_raises_without_base_price_id(self, monkeypatch):
        """An unconfigured base price fails loudly rather than silently
        under-billing an org $20 instead of $70."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_TEAM_BASE_PRICE_ID not configured"):
            await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

    @pytest.mark.asyncio
    async def test_team_checkout_floors_quantity_at_one(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "price_base_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=0)
        assert seen["line_items"] == [
            {"price": "price_base_test", "quantity": 1},
            {"price": "price_seat_test", "quantity": 1},
        ]


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


@pytest.mark.asyncio
async def test_change_provider_trial_local_refuses_use_trial_checkout(monkeypatch):
    """A trial_local row converts via /billing/trial-checkout, never the
    post-onboarding provider-switch path. change_provider must reject it with
    a use_trial_checkout / 409 BEFORE any Stripe or reprovision work.

    (The router maps ChangeProviderError → HTTP 409 with
    detail.code == "use_trial_checkout".)"""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "user_x",
                "subscription_status": "trial_local",
            }
        ),
    )
    # Guards must fire before either of these does any work.
    set_choice = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", set_choice)
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="user_x",
            clerk_user_id="user_x",
            owner_type="personal",
            provider_choice="bedrock_claude",
        )

    assert exc.value.code == "use_trial_checkout"
    assert exc.value.status_code == 409
    # No provider write, no stop, no reprovision dispatch.
    set_choice.assert_not_awaited()
    fake_manager.stop_user_service.assert_not_awaited()
    fake_manager.drain_then_dispatch_provision.assert_not_called()


@pytest.mark.asyncio
async def test_change_provider_into_bedrock_takes_cold_path(monkeypatch):
    """A switch INTO bedrock_claude must NOT take the hot path (which would
    call build_provider_block(bedrock, aip_arns=None) and raise), and must
    NOT raise — it stop-first reprovisions (cold path, F1 fix)."""
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
    # change_provider's cold path lazily imports get_ecs_manager from
    # core.containers, awaits .stop_user_service, and dispatches
    # .drain_then_dispatch_provision(owner_id) as a background task (F1
    # fix) — patch at the source module, and capture the dispatched task
    # instead of scheduling it so nothing outlives the test loop.
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    created: list = []
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
    # No container row: there is nothing to stop, and issuing the stop anyway
    # would open a TOCTOU (a poll creating the row between the fence read and
    # the stop would get an unfenced "stopped" stamp). So the stop is SKIPPED
    # and the cold path falls straight through to the drain-only dispatch,
    # whose create-from-scratch claim provisions the owner.
    fake_manager.stop_user_service.assert_not_awaited()
    fake_manager.drain_then_dispatch_provision.assert_called_once_with("user_x")
    assert len(created) == 1


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
    get_tokens.assert_awaited_once_with(user_id="user_a", bound_to="org_1")
    # Auth file written under the container owner (org_1), not the connector.
    pre_stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "tok", "account_id": "acc"})


@pytest.mark.asyncio
async def test_change_provider_refreshes_owner_facts(monkeypatch):
    """A committed provider switch rewrites the owner-facts file, so agents
    reading it via the isol8 skill see the NEW provider on both paths."""
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
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    facts_mock = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", facts_mock)
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: MagicMock(),
    )

    await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    facts_mock.assert_awaited_once_with(
        "user_x",
        account_overrides={"provider_choice": "bedrock_claude", "byo_provider": None},
    )


@pytest.mark.asyncio
async def test_sync_team_seat_quantity_refreshes_owner_facts(monkeypatch):
    """The membership-change path is the seat-count writer — it must refresh
    the facts file with the new count as an override."""
    # Pin the key OFF: this test stubs get_organization_member_count only, but
    # the seat count now also reads pending invitations. Without this the
    # unstubbed read escapes to api.clerk.com on any machine that exports
    # CLERK_SECRET_KEY. CI happens not to set it; a laptop often does.
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_x",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
                "subscription_item_id": "si_1",
                "seat_count": 2,
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=5))
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", AsyncMock())
    monkeypatch.setattr(billing_service.stripe.SubscriptionItem, "modify", MagicMock())
    facts_mock = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", facts_mock)

    count = await billing_service.sync_team_seat_quantity("org_x")

    assert count == 5
    facts_mock.assert_awaited_once_with("org_x", account_overrides={"seat_count": 5})


class TestCheckoutSlotClaim:
    """Per-owner serialization of Checkout session creation — the guard
    against two concurrent different-provider requests each minting a
    completable session (different idempotency keys, so Stripe won't
    collapse them)."""

    @pytest.mark.asyncio
    async def test_claim_denied_raises_checkout_in_progress(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        with (
            patch.object(
                billing_service.billing_repo,
                "try_claim_checkout_slot",
                new=AsyncMock(return_value=False),
            ),
            patch.object(billing_service, "_build_subscription_checkout", new=AsyncMock()) as mock_build,
            pytest.raises(BillingServiceError, match="checkout_in_progress"),
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1")
        mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_write_failure_fails_closed(self, monkeypatch):
        """An unrecorded open session is invisible to every guard once the
        claim TTL lapses, yet stays completable for ~31 minutes. On marker
        failure the session (whose URL the user never saw) must be expired,
        the slot released, and the request failed."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        fake_session = MagicMock(url="https://checkout/x", id="cs_unrecorded")
        with (
            patch.object(billing_service.stripe.checkout.Session, "create", return_value=fake_session),
            patch.object(billing_service.stripe.checkout.Session, "expire") as mock_expire,
            patch.object(
                billing_service.billing_repo,
                "get_by_owner_id",
                new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
            ),
            patch.object(
                billing_service.billing_repo,
                "set_pending_checkout",
                new=AsyncMock(side_effect=RuntimeError("ddb down")),
            ),
            patch.object(
                billing_service.billing_repo,
                "release_checkout_slot",
                new=AsyncMock(),
            ) as mock_release,
            pytest.raises(BillingServiceError, match="checkout_unavailable"),
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1")

        mock_expire.assert_called_once_with("cs_unrecorded")
        mock_release.assert_awaited_once_with("u_1")

    @pytest.mark.asyncio
    async def test_build_failure_releases_slot(self, monkeypatch):
        """A failed session create must free the claim so the user's retry
        doesn't wait out the 90s TTL."""
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        with (
            patch.object(
                billing_service.billing_repo,
                "release_checkout_slot",
                new=AsyncMock(),
            ) as mock_release,
            patch.object(
                billing_service,
                "_build_subscription_checkout",
                new=AsyncMock(side_effect=BillingServiceError("No Stripe customer for owner_id=u_1")),
            ),
            pytest.raises(BillingServiceError, match="No Stripe customer"),
        ):
            await billing_service.create_flat_fee_checkout(owner_id="u_1")
        mock_release.assert_awaited_once_with("u_1")


class TestCreditTopUpProvisionsOnDemand:
    """`create_credit_top_up_checkout` must PROVISION the owner's Metronome
    ledger, not merely assert someone else already did.

    It used to just check the ids were on the billing row and raise "try again
    in a moment" otherwise, on the stated reasoning that "provisioning re-runs
    on every status poll". That is false for a personal owner with a running
    container: `ensure_metronome_customer`'s only other call site is
    `ensure_user_ready`, which `routers/container.py` re-dispatches on a running
    box solely for org context or a failed Teams seat. A trial->paid converted
    owner therefore sat un-provisioned forever and got "try again in a moment"
    on every attempt — on the exact screen the credits gate sends them to, so
    the one action that would fix their account was the one action they could
    not complete.
    """

    @pytest.fixture
    def _account(self):
        return {
            "owner_id": "user_x",
            "stripe_customer_id": "cus_x",
        }

    @pytest.mark.asyncio
    async def test_provisions_then_proceeds_when_ids_are_missing(self, _account, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "METRONOME_CREDITS_PRODUCT_ID", "prod_x")
        provisioned = {
            **_account,
            "metronome_customer_id": "mc-1",
            "metronome_contract_id": "ct-1",
        }
        # First read: un-provisioned. Second read (after ensuring): provisioned.
        reads = [dict(_account), provisioned]
        ensure = AsyncMock(return_value="mc-1")
        with (
            patch.object(billing_service.billing_repo, "get_by_owner_id", new=AsyncMock(side_effect=reads)),
            patch("core.billing.metronome_provisioning.ensure_metronome_customer", new=ensure),
            patch("stripe.checkout.Session.create", return_value=MagicMock(url="https://checkout/credits")),
        ):
            session = await billing_service.create_credit_top_up_checkout(owner_id="user_x", amount_cents=2000)

        ensure.assert_awaited_once_with(owner_id="user_x")
        assert session.url == "https://checkout/credits"

    @pytest.mark.asyncio
    async def test_still_refuses_to_take_money_when_provisioning_fails(self, _account, monkeypatch):
        """The money guard survives: if provisioning can't complete, no payable
        session is created. Collecting first and failing the grant afterwards
        strands the customer's money — the original reason this check exists."""
        monkeypatch.setattr(billing_service.settings, "METRONOME_CREDITS_PRODUCT_ID", "prod_x")
        create = MagicMock()
        with (
            patch.object(billing_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value=dict(_account))),
            patch(
                "core.billing.metronome_provisioning.ensure_metronome_customer",
                new=AsyncMock(side_effect=RuntimeError("metronome down")),
            ),
            patch("stripe.checkout.Session.create", new=create),
        ):
            with pytest.raises(BillingServiceError, match="contact support"):
                await billing_service.create_credit_top_up_checkout(owner_id="user_x", amount_cents=2000)

        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_permanent_failure_is_not_reported_as_transient(self, _account, monkeypatch):
        """A provisioning call that RAISED will raise again on every retry, so
        "try again in a moment" sends the buyer into an infinite loop with
        nothing surfaced to them or support saying it can never work. The
        transient wording is reserved for the non-raising case."""
        monkeypatch.setattr(billing_service.settings, "METRONOME_CREDITS_PRODUCT_ID", "prod_x")
        with (
            patch.object(billing_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value=dict(_account))),
            patch(
                "core.billing.metronome_provisioning.ensure_metronome_customer",
                new=AsyncMock(side_effect=RuntimeError("metronome down")),
            ),
        ):
            with pytest.raises(BillingServiceError) as exc:
                await billing_service.create_credit_top_up_checkout(owner_id="user_x", amount_cents=2000)
        assert "try again in a moment" not in str(exc.value)
        assert "support@isol8.co" in str(exc.value)

    @pytest.mark.asyncio
    async def test_silent_no_op_still_reports_transient(self, _account, monkeypatch):
        """Provisioning returned without raising but the ids still aren't
        visible — a moment genuinely can fix that, so keep the retry wording."""
        monkeypatch.setattr(billing_service.settings, "METRONOME_CREDITS_PRODUCT_ID", "prod_x")
        with (
            patch.object(billing_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value=dict(_account))),
            patch("core.billing.metronome_provisioning.ensure_metronome_customer", new=AsyncMock(return_value=None)),
        ):
            with pytest.raises(BillingServiceError, match="try again in a moment"):
                await billing_service.create_credit_top_up_checkout(owner_id="user_x", amount_cents=2000)

    @pytest.mark.asyncio
    async def test_no_op_when_already_provisioned(self, _account, monkeypatch):
        """Idempotent and cheap on the common path — an already-provisioned
        owner must not pay for a Metronome round trip per top-up."""
        monkeypatch.setattr(billing_service.settings, "METRONOME_CREDITS_PRODUCT_ID", "prod_x")
        account = {**_account, "metronome_customer_id": "mc-1", "metronome_contract_id": "ct-1"}
        ensure = AsyncMock()
        with (
            patch.object(billing_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value=account)),
            patch("core.billing.metronome_provisioning.ensure_metronome_customer", new=ensure),
            patch("stripe.checkout.Session.create", return_value=MagicMock(url="https://checkout/credits")),
        ):
            await billing_service.create_credit_top_up_checkout(owner_id="user_x", amount_cents=2000)

        ensure.assert_not_awaited()
