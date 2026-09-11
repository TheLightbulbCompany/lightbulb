"""Tests for the two-endpoint webhook registration, source-prefixed dedup
keying, the platform fee, and the fulfillment-time payout flush."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _webhook_secrets(monkeypatch):
    """The webhook route skips empty secrets entirely (fail-closed), so the
    hermetic default of "" would 400 every test before construct_event's mock
    is reached. Both secrets set: the route tries MARKETPLACE first."""
    from core.config import settings

    monkeypatch.setattr(settings, "STRIPE_MARKETPLACE_WEBHOOK_SECRET", "whsec_mkt_test")
    monkeypatch.setattr(settings, "STRIPE_CONNECT_WEBHOOK_SECRET", "whsec_connect_test")


@pytest.fixture(autouse=True)
def _no_existing_transfers(monkeypatch):
    """The flush adopts an existing Transfer for the purchase's group before
    creating one; default every test to "none found" so create-path
    expectations hold. Adoption tests override."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services import payout_service as _ps

    monkeypatch.setattr(_ps, "find_transfer_id_by_group", _AsyncMock(return_value=None))


@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_falls_back_to_connect_secret(mock_construct, mock_dedup, client):
    """Two Stripe endpoint registrations (platform-scoped + Connect-scoped)
    deliver to this URL with different signing secrets. A Connect-signed
    event fails against the marketplace secret and must verify against the
    Connect secret on the second try."""
    import stripe as stripe_mod

    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.ALREADY_SEEN  # short-circuit handling
    sig_err = stripe_mod.error.SignatureVerificationError("bad sig", "hdr")
    mock_construct.side_effect = [
        sig_err,
        {"id": "evt_c", "type": "account.updated", "data": {"object": {}}},
    ]

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_c"}',
    )
    assert resp.status_code == 200, resp.text
    secrets_tried = [call.args[2] for call in mock_construct.call_args_list]
    assert secrets_tried == ["whsec_mkt_test", "whsec_connect_test"]


@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_rejects_when_both_secrets_fail(mock_construct, client):
    import stripe as stripe_mod

    mock_construct.side_effect = stripe_mod.error.SignatureVerificationError("bad sig", "hdr")
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_x"}',
    )
    assert resp.status_code == 400


@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_dedup_key_is_source_prefixed(mock_construct, mock_dedup, client):
    """Stripe mints ONE event id per occurrence and delivers it to every
    subscribed endpoint; the billing webhook subscribes to
    checkout.session.completed too. Bare event-id keying in the shared dedup
    table let whichever endpoint claimed first swallow the other's processing
    (lost purchase or lost credit grant)."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.ALREADY_SEEN
    mock_construct.return_value = {
        "id": "evt_shared",
        "type": "checkout.session.completed",
        "data": {"object": {}},
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_shared"}',
    )
    assert resp.status_code == 200
    assert mock_dedup.call_args.kwargs["event_id"] == "stripe-marketplace:evt_shared"


@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
def test_marketplace_webhook_ignores_unrelated_checkout_session(mock_dedup, mock_construct, mock_boto_client, client):
    """With the dedup collision fixed, BOTH endpoints genuinely process every
    checkout.session.completed. An unrelated session carries no listing
    metadata and must be acknowledged without writing a purchase."""
    fake_ddb = MagicMock()
    mock_boto_client.return_value = fake_ddb
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_billing_shape",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_unrelated",
                "payment_status": "paid",
                "metadata": {"owner_id": "u1"},
                "amount_total": 5000,
            }
        },
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_billing_shape"}',
    )
    assert resp.status_code == 200, resp.text
    fake_ddb.transact_write_items.assert_not_called()


@patch("routers.marketplace_purchases._flush_seller_held_balance", new_callable=AsyncMock)
@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_checkout_completed_credits_seller_share_not_full_price(
    mock_gen, mock_dedup, mock_construct, mock_pi_retrieve, mock_boto_client, mock_pa_table, mock_flush, client
):
    """15% platform fee: the buyer pays amount_total; the seller's held
    balance is credited the post-fee share, and the purchase row records
    both figures."""
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_111"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
    mock_pa_table.return_value.get_item = MagicMock(return_value={})  # no payout account yet
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_fee",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_fee",
                "payment_status": "paid",
                "metadata": {"listing_id": "l1", "buyer_id": "b1", "seller_id": "s1", "version": "1"},
                "amount_total": 2000,
                "payment_intent": "pi_1",
            }
        },
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_fee"}',
    )
    assert resp.status_code == 200, resp.text
    transact = fake_ddb.transact_write_items.call_args.kwargs["TransactItems"]
    put_item = transact[0]["Put"]["Item"]
    assert put_item["price_paid_cents"] == {"N": "2000"}
    assert put_item["seller_share_cents"] == {"N": "1700"}
    credit = transact[1]["Update"]["ExpressionAttributeValues"][":amt"]
    assert credit == {"N": "1700"}


@patch("routers.marketplace_purchases._flush_seller_held_balance", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.stripe.Account.retrieve")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_checkout_completed_flushes_when_seller_already_payable(
    mock_gen,
    mock_dedup,
    mock_construct,
    mock_pi_retrieve,
    mock_boto_client,
    mock_pa_table,
    mock_acct,
    mock_flush,
    client,
):
    """A seller who onboarded BEFORE this sale never gets another
    account.updated — fulfillment itself must flush the held balance."""
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_111"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={"Item": {"seller_id": "s1", "stripe_connect_account_id": "acct_9", "balance_held_cents": 1700}}
    )
    mock_acct.return_value = {"id": "acct_9", "payouts_enabled": True}
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_flush",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_flush",
                "payment_status": "paid",
                "metadata": {"listing_id": "l1", "buyer_id": "b1", "seller_id": "s1", "version": "1"},
                "amount_total": 2000,
                "payment_intent": "pi_1",
            }
        },
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_flush"}',
    )
    assert resp.status_code == 200, resp.text
    # The just-fulfilled purchase is passed explicitly — the eventually
    # consistent listing index may not surface it yet.
    mock_flush.assert_awaited_once_with(
        seller_id="s1", connect_account_id="acct_9", known_purchase_key=("b1", "cs_cs_flush")
    )


@patch("routers.marketplace_purchases.webhook_dedup.delete_event")
@patch("routers.marketplace_purchases._flush_seller_held_balance", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.stripe.Account.retrieve")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_flush_failure_propagates_so_stripe_retries(
    mock_gen,
    mock_dedup,
    mock_construct,
    mock_pi_retrieve,
    mock_boto_client,
    mock_pa_table,
    mock_acct,
    mock_flush,
    mock_delete,
    client,
):
    """A swallowed flush error would ack the webhook with a transferred-but-
    unstamped purchase and no prompt retry — reopening the >24h Stripe-
    idempotency double-pay window. The failure must propagate so the dedup
    rollback fires and Stripe redelivers (fulfillment is idempotent)."""
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_111"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={"Item": {"seller_id": "s1", "stripe_connect_account_id": "acct_9", "balance_held_cents": 1700}}
    )
    mock_acct.return_value = {"id": "acct_9", "payouts_enabled": True}
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_flush.side_effect = RuntimeError("transient DDB error mid-flush")

    async def del_ok(*_a, **_k):
        return None

    mock_delete.side_effect = del_ok
    mock_construct.return_value = {
        "id": "evt_flush_fail",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_ff",
                "payment_status": "paid",
                "metadata": {"listing_id": "l1", "buyer_id": "b1", "seller_id": "s1", "version": "1"},
                "amount_total": 2000,
                "payment_intent": "pi_1",
            }
        },
    }

    from main import app

    raising_client = TestClient(app, raise_server_exceptions=False)
    resp = raising_client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_flush_fail"}',
    )
    assert resp.status_code == 500
    mock_delete.assert_called_once_with(event_id="stripe-marketplace:evt_flush_fail")


@pytest.mark.asyncio
async def test_flush_processes_known_purchase_missing_from_index():
    """The eventually-consistent listing index can miss the purchase
    fulfillment just wrote; the explicit known_purchase_key must be processed
    via a consistent base-table read anyway."""
    from routers.marketplace_purchases import _flush_seller_held_balance

    purchase_row = {
        "buyer_id": "b1",
        "purchase_id": "cs_x",
        "seller_share_cents": 1700,
        "price_paid_cents": 2000,
        "stripe_transfer_group": "tg_x",
    }
    with (
        patch("routers.marketplace_purchases.marketplace_repo.get_payout_account", new_callable=AsyncMock) as mock_pa,
        patch("routers.marketplace_purchases.marketplace_repo.get_purchase", new_callable=AsyncMock) as mock_get,
        patch(
            "routers.marketplace_purchases.marketplace_repo.list_listings_by_seller",
            new_callable=AsyncMock,
        ) as mock_listings,
        patch(
            "routers.marketplace_purchases.marketplace_repo.set_seller_transfer_if_absent",
            new_callable=AsyncMock,
        ) as mock_stamp,
        patch(
            "routers.marketplace_purchases.marketplace_repo.apply_flush_totals", new_callable=AsyncMock
        ) as mock_totals,
        patch(
            "routers.marketplace_purchases.marketplace_repo.mark_payout_accounted", new_callable=AsyncMock
        ) as mock_mark,
        patch(
            "routers.marketplace_purchases.payout_service.transfer_held_balance", new_callable=AsyncMock
        ) as mock_transfer,
    ):
        # Eventual reads show NOTHING yet: zero balance, no listings.
        mock_pa.return_value = {"seller_id": "s1", "balance_held_cents": 0}
        mock_listings.return_value = []
        mock_get.return_value = purchase_row
        mock_transfer.return_value = "tr_new"
        mock_stamp.return_value = True

        await _flush_seller_held_balance(seller_id="s1", connect_account_id="acct_1", known_purchase_key=("b1", "cs_x"))

    # Two consistent reads: the known-purchase fetch, then the post-stamp
    # refund-race re-check.
    assert mock_get.await_count == 2
    assert mock_get.await_args_list[0].args == ("b1", "cs_x")
    mock_transfer.assert_awaited_once_with(connect_account_id="acct_1", amount_cents=1700, transfer_group="tg_x")
    mock_totals.assert_awaited_once_with(seller_id="s1", transferred_cents=1700)
    mock_mark.assert_awaited_once_with(buyer_id="b1", purchase_id="cs_x")


@pytest.mark.asyncio
async def test_flush_reverses_transfer_when_refund_races_the_stamp():
    """Regression (codex): charge.refunded can observe the row UNstamped,
    revoke it, and debit only the held balance — while this flush's transfer
    completes and stamps after. Without the post-stamp re-check the seller
    stays paid with no later event to reverse it. The flush must issue the
    compensating reversal (same idempotency key as the refund paths) and must
    NOT book the transfer as settled proceeds."""
    from routers.marketplace_purchases import _flush_seller_held_balance

    active_row = {
        "buyer_id": "b1",
        "purchase_id": "cs_r",
        "seller_share_cents": 1700,
        "stripe_transfer_group": "tg_r",
    }
    revoked_row = dict(active_row, seller_transfer_id="tr_race", license_key_revoked=True)
    with (
        patch("routers.marketplace_purchases.marketplace_repo.get_payout_account", new_callable=AsyncMock) as mock_pa,
        patch("routers.marketplace_purchases.marketplace_repo.get_purchase", new_callable=AsyncMock) as mock_get,
        patch(
            "routers.marketplace_purchases.marketplace_repo.list_listings_by_seller",
            new_callable=AsyncMock,
        ) as mock_listings,
        patch(
            "routers.marketplace_purchases.marketplace_repo.set_seller_transfer_if_absent",
            new_callable=AsyncMock,
        ) as mock_stamp,
        patch(
            "routers.marketplace_purchases.marketplace_repo.apply_flush_totals", new_callable=AsyncMock
        ) as mock_totals,
        patch(
            "routers.marketplace_purchases.marketplace_repo.mark_payout_accounted", new_callable=AsyncMock
        ) as mock_mark,
        patch(
            "routers.marketplace_purchases.marketplace_repo.decrement_held_balance", new_callable=AsyncMock
        ) as mock_debit,
        patch(
            "routers.marketplace_purchases.payout_service.transfer_held_balance", new_callable=AsyncMock
        ) as mock_transfer,
        patch("routers.marketplace_purchases.stripe.Transfer.create_reversal") as mock_reversal,
    ):
        mock_pa.return_value = {"seller_id": "s1", "balance_held_cents": 1700}
        mock_listings.return_value = []
        # First read: active. Post-stamp re-check: refund won the race.
        mock_get.side_effect = [active_row, revoked_row]
        mock_transfer.return_value = "tr_race"
        mock_stamp.return_value = True
        mock_mark.return_value = True

        await _flush_seller_held_balance(seller_id="s1", connect_account_id="acct_1", known_purchase_key=("b1", "cs_r"))

    mock_reversal.assert_called_once()
    args, kwargs = mock_reversal.call_args
    assert args[0] == "tr_race"
    assert kwargs["amount"] == 1700
    assert kwargs["idempotency_key"] == "reversal:tr_race"
    # No proceeds booked; the settlement claim owns the held-balance debit
    # exactly once (the refund handler saw the row unstamped and skipped it).
    mock_totals.assert_not_awaited()
    mock_mark.assert_awaited_once_with(buyer_id="b1", purchase_id="cs_r")
    mock_debit.assert_awaited_once_with(seller_id="s1", amount_cents=1700)


@pytest.mark.asyncio
async def test_flush_recovery_rereverses_revoked_transfer_instead_of_booking():
    """Regression (codex): a crash between the compensating reversal and its
    accounting claim leaves a stamped, REVOKED, unaccounted purchase. The
    recovery path must re-inspect the revoked state and re-issue the
    (idempotent) reversal — booking it as proceeds would raise lifetime
    earnings for a transfer that was already clawed back."""
    from routers.marketplace_purchases import _flush_seller_held_balance

    reversed_crash_row = {
        "buyer_id": "b1",
        "purchase_id": "cs_c",
        "seller_share_cents": 1700,
        "stripe_transfer_group": "tg_c",
        "seller_transfer_id": "tr_c",
        "license_key_revoked": True,
        # no payout_accounted — the crash signature
    }
    with (
        patch("routers.marketplace_purchases.marketplace_repo.get_payout_account", new_callable=AsyncMock) as mock_pa,
        patch("routers.marketplace_purchases.marketplace_repo.get_purchase", new_callable=AsyncMock) as mock_get,
        patch(
            "routers.marketplace_purchases.marketplace_repo.list_listings_by_seller",
            new_callable=AsyncMock,
        ) as mock_listings,
        patch(
            "routers.marketplace_purchases.marketplace_repo.apply_flush_totals", new_callable=AsyncMock
        ) as mock_totals,
        patch(
            "routers.marketplace_purchases.marketplace_repo.mark_payout_accounted", new_callable=AsyncMock
        ) as mock_mark,
        patch(
            "routers.marketplace_purchases.marketplace_repo.decrement_held_balance", new_callable=AsyncMock
        ) as mock_debit,
        patch(
            "routers.marketplace_purchases.payout_service.transfer_held_balance", new_callable=AsyncMock
        ) as mock_transfer,
        patch("routers.marketplace_purchases.stripe.Transfer.create_reversal") as mock_reversal,
    ):
        mock_pa.return_value = {"seller_id": "s1", "balance_held_cents": 1700}
        mock_listings.return_value = []
        mock_get.return_value = reversed_crash_row
        mock_mark.return_value = True

        await _flush_seller_held_balance(seller_id="s1", connect_account_id="acct_1", known_purchase_key=("b1", "cs_c"))

    mock_transfer.assert_not_awaited()  # already transferred — never again
    mock_reversal.assert_called_once()
    assert mock_reversal.call_args.kwargs["idempotency_key"] == "reversal:tr_c"
    mock_totals.assert_not_awaited()
    mock_mark.assert_awaited_once_with(buyer_id="b1", purchase_id="cs_c")
    # The crash happened before the claim, so the refund handler never
    # debited (it saw the row stamped) — the recovery's claim does it.
    mock_debit.assert_awaited_once_with(seller_id="s1", amount_cents=1700)


@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("core.repositories.marketplace_repo._listings_table")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("routers.marketplace_purchases.billing_repo.get_by_company_stripe_account_id")
def test_flush_skips_purchase_when_fresh_read_shows_transfer(
    mock_company_lookup,
    mock_pa_table,
    mock_purchases_table,
    mock_dedup,
    mock_construct,
    mock_listings_table,
    mock_transfer,
    client,
):
    """The listing-created-index feeding the flush loop is eventually
    consistent, and Stripe idempotency keys expire after 24h while webhook
    retries run for days — the strongly-consistent re-read right before
    Transfer.create is the guard against a stale-index double-pay."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_company_lookup.return_value = None  # seller account, not company
    mock_construct.return_value = {
        "id": "evt_acct_fresh",
        "type": "account.updated",
        "data": {"object": {"id": "acct_1", "payouts_enabled": True, "metadata": {"seller_id": "s1"}}},
    }
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={"Item": {"seller_id": "s1", "balance_held_cents": 2000, "stripe_connect_account_id": "acct_1"}}
    )
    mock_pa_table.return_value.update_item = MagicMock()
    listings_table = MagicMock()
    listings_table.query = MagicMock(return_value={"Items": [{"listing_id": "l1", "seller_id": "s1"}]})
    mock_listings_table.return_value = listings_table
    # Index says untransferred…
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {"buyer_id": "b1", "purchase_id": "p1", "price_paid_cents": 2000, "stripe_transfer_group": "tg_1"}
            ]
        }
    )
    # …but the fresh base-table read shows a prior run already paid it.
    mock_purchases_table.return_value.get_item = MagicMock(
        return_value={"Item": {"buyer_id": "b1", "purchase_id": "p1", "seller_transfer_id": "tr_done"}}
    )

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_acct_fresh"}',
    )
    assert resp.status_code == 200, resp.text
    mock_transfer.assert_not_called()
    # No transfers → no flush-totals write.
    mock_pa_table.return_value.update_item.assert_not_called()


@pytest.mark.asyncio
async def test_flush_adopts_existing_transfer_instead_of_creating(monkeypatch):
    """Regression (codex, PR #1751): a transfer that succeeded but crashed
    before its stamp landed — or a retry straddling the idempotency-key day
    boundary — must be ADOPTED via the transfer_group lookup, never minted a
    second time. The set-once stamp then records it."""
    from routers.marketplace_purchases import _flush_seller_held_balance

    purchase_row = {
        "buyer_id": "b1",
        "purchase_id": "cs_a",
        "seller_share_cents": 850,
        "stripe_transfer_group": "tg_a",
    }
    with (
        patch("routers.marketplace_purchases.marketplace_repo.get_payout_account", new_callable=AsyncMock) as mock_pa,
        patch("routers.marketplace_purchases.marketplace_repo.get_purchase", new_callable=AsyncMock) as mock_get,
        patch(
            "routers.marketplace_purchases.marketplace_repo.list_listings_by_seller",
            new_callable=AsyncMock,
        ) as mock_listings,
        patch(
            "routers.marketplace_purchases.marketplace_repo.set_seller_transfer_if_absent",
            new_callable=AsyncMock,
        ) as mock_stamp,
        patch(
            "routers.marketplace_purchases.marketplace_repo.apply_flush_totals", new_callable=AsyncMock
        ) as mock_totals,
        patch(
            "routers.marketplace_purchases.marketplace_repo.mark_payout_accounted", new_callable=AsyncMock
        ) as mock_mark,
        patch(
            "routers.marketplace_purchases.payout_service.find_transfer_id_by_group", new_callable=AsyncMock
        ) as mock_find,
        patch(
            "routers.marketplace_purchases.payout_service.transfer_held_balance", new_callable=AsyncMock
        ) as mock_transfer,
    ):
        mock_pa.return_value = {"seller_id": "s1", "balance_held_cents": 850}
        mock_listings.return_value = []
        mock_get.return_value = purchase_row
        mock_find.return_value = "tr_orphaned"
        mock_stamp.return_value = True
        mock_mark.return_value = True

        await _flush_seller_held_balance(seller_id="s1", connect_account_id="acct_1", known_purchase_key=("b1", "cs_a"))

    mock_find.assert_awaited_once_with(transfer_group="tg_a")
    mock_transfer.assert_not_awaited()
    assert mock_stamp.await_args.kwargs["transfer_id"] == "tr_orphaned"
    mock_totals.assert_awaited_once_with(seller_id="s1", transferred_cents=850)
