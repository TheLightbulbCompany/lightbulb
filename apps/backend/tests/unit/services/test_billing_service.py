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
            result = await billing_service.create_flat_fee_checkout(owner_id="u_1", provider_choice="bedrock_claude")

        assert result.url == "https://checkout/flat"
        assert seen["price_id"] == "price_flat_test"
        assert seen["quantity"] == 1
        assert seen["trial_days"] == 14
        assert seen["metadata"]["owner_id"] == "u_1"
        assert seen["metadata"]["provider_choice"] == "bedrock_claude"
        assert seen["idempotency_key"].startswith("flat_checkout:u_1:")

    @pytest.mark.asyncio
    async def test_team_checkout_uses_seat_price_quantity_and_no_trial(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team", id="cs_team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        result = await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

        assert result.url == "https://checkout/team"
        assert seen["price_id"] == "price_seat_test"
        assert seen["quantity"] == 4
        assert seen["trial_days"] is None
        # provider_choice threaded in so the webhook backup persists teams as
        # Bedrock-managed (there is no provider picker in the team flow).
        assert seen["metadata"] == {
            "owner_id": "org_1",
            "purpose": "team_seat",
            "provider_choice": "bedrock_claude",
        }
        assert seen["idempotency_key"].startswith("team_checkout:org_1:")

    @pytest.mark.asyncio
    async def test_team_checkout_raises_without_seat_price_id(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_TEAM_SEAT_PRICE_ID not configured"):
            await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

    @pytest.mark.asyncio
    async def test_team_checkout_floors_quantity_at_one(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=0)
        assert seen["quantity"] == 1


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
    bg = AsyncMock()
    monkeypatch.setattr("core.services.container_provision.background_provision", bg)
    created: list = []
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: created.append(coro) or MagicMock(),
    )

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="user_x",
            clerk_user_id="user_x",
            owner_type="personal",
            provider_choice="bedrock_claude",
        )

    assert exc.value.code == "use_trial_checkout"
    assert exc.value.status_code == 409
    # No provider write, no reprovision dispatch.
    set_choice.assert_not_awaited()
    bg.assert_not_called()
    assert created == []


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
    monkeypatch.setattr("core.services.container_provision.background_provision", AsyncMock())
    created = []
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: created.append(coro) or MagicMock(),
    )

    await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    facts_mock.assert_awaited_once_with("user_x")
    for coro in created:
        coro.close()
