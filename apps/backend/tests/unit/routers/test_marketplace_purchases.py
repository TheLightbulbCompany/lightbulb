"""Tests for marketplace_purchases router."""

import os

from core.config import apex_domain, settings, url_host

# The return-URL allowlist derives from settings.FRONTEND_URL, so the fixtures
# must derive too — literals matched only while the configured domain equalled them.
RETURN_APEX = apex_domain(url_host(settings.FRONTEND_URL))

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
def _listing_matches_checkout(monkeypatch):
    """Fulfillment checks session metadata against the listing row. Every
    checkout in this file sells listing l1 v1 by seller s1 at 2000 cents."""
    from core.repositories import marketplace_repo as _repo

    monkeypatch.setattr(
        _repo,
        "get_listing",
        AsyncMock(return_value={"listing_id": "l1", "version": 1, "seller_id": "s1", "price_cents": 2000}),
    )


@pytest.fixture(autouse=True)
def _not_a_company_account(monkeypatch):
    """`account.updated` now asks the billing GSI whether the account is a
    company account before taking the seller path. Unpatched that is a live
    DynamoDB Query. The seller cases in this file are all "not a company";
    the company branch has its own file."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.repositories import billing_repo as _billing_repo

    monkeypatch.setattr(_billing_repo, "get_by_company_stripe_account_id", _AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _no_existing_transfers(monkeypatch):
    """The flush adopts an existing Transfer for the purchase's group before
    creating one; default every test to "none found" so create-path
    expectations hold. Adoption tests override."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services import payout_service as _ps

    monkeypatch.setattr(_ps, "find_transfer_id_by_group", _AsyncMock(return_value=None))


@patch("core.repositories.marketplace_repo._listings_table")
@patch("core.repositories.marketplace_repo._purchases_table")
def test_my_purchases_sorts_by_created_at_not_uuid(mock_purchases_table, mock_listings_table, client):
    """Regression: /my-purchases must sort by created_at, not by purchase_id
    (UUID, the table's sort key). ScanIndexForward=False on the base table
    returns reverse-lexical UUID order, not newest-first.
    """
    from main import app

    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="human_b1", org_id="b1", org_role="org:admin"
    )

    # UUIDs out-of-order vs. created_at: ensures we're not lucky-sorting.
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "zzz_uuid",  # high lexical
                    "listing_id": "l1",
                    "license_key": "iml_a",
                    "price_paid_cents": 100,
                    "status": "paid",
                    "created_at": "2026-01-01T00:00:00Z",  # OLD
                },
                {
                    "buyer_id": "b1",
                    "purchase_id": "aaa_uuid",  # low lexical
                    "listing_id": "l1",
                    "license_key": "iml_b",
                    "price_paid_cents": 200,
                    "status": "paid",
                    "created_at": "2026-04-15T00:00:00Z",  # NEW
                },
            ]
        }
    )
    listings_table = MagicMock()
    listings_table.get_item.return_value = {"Item": {"slug": "demo"}}
    mock_listings_table.return_value = listings_table

    resp = client.get("/api/v1/marketplace/my-purchases")
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    # Newest first — created_at desc, NOT lexical UUID desc.
    assert items[0]["created_at"] == "2026-04-15T00:00:00Z"
    assert items[0]["license_key"] == "iml_b"
    assert items[1]["created_at"] == "2026-01-01T00:00:00Z"


@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_webhook_checkout_completed_grants_license(
    mock_gen, mock_dedup, mock_construct, mock_pi_retrieve, mock_boto_client, mock_pa_table, client
):
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_111"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
    # No payout account: the fulfillment-time flush no-ops.
    mock_pa_table.return_value.get_item = MagicMock(return_value={})
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "payment_status": "paid",
                "metadata": {
                    "listing_id": "l1",
                    "buyer_id": "b1",
                    "seller_id": "s1",
                    "version": "1",
                },
                "amount_total": 2000,
                "payment_intent": "pi_1",
            }
        },
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_1","type":"checkout.session.completed"}',
    )
    assert resp.status_code == 200
    fake_ddb.transact_write_items.assert_called_once()
    transact = fake_ddb.transact_write_items.call_args.kwargs["TransactItems"]
    # Atomic: Put purchase + Update payout-accounts in one transaction.
    assert len(transact) == 2
    assert transact[0]["Put"]["ConditionExpression"] == "attribute_not_exists(purchase_id)"


@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_idempotent_on_replay(mock_construct, mock_dedup, client):
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.ALREADY_SEEN
    mock_construct.return_value = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {"object": {}},
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_1"}',
    )
    assert resp.status_code == 200


@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_webhook_skips_fulfillment_for_unpaid_delayed_payment(
    mock_pa_table, mock_purchases_table, mock_gen, mock_dedup, mock_construct, client
):
    """Regression: Stripe emits checkout.session.completed with
    payment_status='unpaid' for delayed-payment methods (ACH, BNPL); the
    actual settlement comes later as async_payment_succeeded. The fulfillment
    path must gate on payment_status='paid' or buyers receive entitlement on
    payments that may later fail.
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_async_unpaid",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_async",
                "payment_status": "unpaid",
                "metadata": {
                    "listing_id": "l1",
                    "buyer_id": "b1",
                    "seller_id": "s1",
                    "version": "1",
                },
                "amount_total": 2000,
                "payment_intent": "pi_async",
            }
        },
    }
    mock_purchases_table.return_value.put_item = MagicMock()
    mock_pa_table.return_value.update_item = MagicMock()

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_async_unpaid"}',
    )
    assert resp.status_code == 200
    # Critical assertions: NO purchase row written, NO seller balance bump.
    mock_purchases_table.return_value.put_item.assert_not_called()
    mock_pa_table.return_value.update_item.assert_not_called()


@patch("core.repositories.marketplace_repo._payout_accounts_table")
@patch("core.repositories.marketplace_repo._ddb_client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_webhook_async_payment_succeeded_grants_license(
    mock_gen, mock_dedup, mock_construct, mock_pi_retrieve, mock_boto_client, mock_pa_table, client
):
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_async"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
    mock_pa_table.return_value.get_item = MagicMock(return_value={})
    """Regression: async_payment_succeeded is the settle event for delayed
    methods. Must run the same fulfillment path as a synchronous paid
    checkout.session.completed."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_async_paid",
        "type": "checkout.session.async_payment_succeeded",
        "data": {
            "object": {
                "id": "cs_async",
                "payment_status": "paid",
                "metadata": {
                    "listing_id": "l1",
                    "buyer_id": "b1",
                    "seller_id": "s1",
                    "version": "1",
                },
                "amount_total": 2000,
                "payment_intent": "pi_async",
            }
        },
    }
    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_async_paid"}',
    )
    assert resp.status_code == 200
    fake_ddb.transact_write_items.assert_called_once()


@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.revoke")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_charge_refunded_debits_held_balance_when_pre_payout(
    mock_pa_table, mock_purchases_table, mock_revoke, mock_dedup, mock_construct, client
):
    """Regression: charge.refunded must subtract from balance_held_cents
    when the purchase hasn't been transferred yet. Otherwise the
    skip-refunded-in-flush path leaves the held balance permanently inflated.
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_pre_payout",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_1", "refunded": True}},
    }
    # Purchase row WITHOUT seller_transfer_id — refund happened before payout.
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "listing_id": "l1",
                    "seller_id": "s1",
                    "price_paid_cents": 2000,
                    "license_key_revoked": False,
                    # no seller_transfer_id => money still in held balance
                }
            ]
        }
    )

    async def fake_revoke(*, purchase_id, buyer_id, reason):
        return None

    mock_revoke.side_effect = fake_revoke
    mock_pa_table.return_value.update_item = MagicMock()

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_refund_pre_payout"}',
    )
    assert resp.status_code == 200, resp.text

    # License was revoked AND held balance was debited by the purchase amount.
    mock_revoke.assert_awaited_once()
    mock_pa_table.return_value.update_item.assert_called_once()
    pa_update = mock_pa_table.return_value.update_item.call_args.kwargs
    assert pa_update["Key"] == {"seller_id": "s1"}
    assert pa_update["ExpressionAttributeValues"][":amt"] == 2000
    assert "balance_held_cents - :amt" in pa_update["UpdateExpression"]


@patch("routers.marketplace_purchases.stripe.Transfer.create_reversal")
@patch("routers.marketplace_purchases.stripe.Transfer.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.revoke")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_charge_refunded_already_booked_transfer_reverses_idempotently_without_debit(
    mock_pa_table,
    mock_purchases_table,
    mock_revoke,
    mock_dedup,
    mock_construct,
    mock_tr_retrieve,
    mock_reversal,
    client,
):
    """When the purchase was already transferred AND booked
    (payout_accounted), the flush's totals already moved the share out of the
    held balance — charge.refunded re-issues the idempotent reversal (a
    Stripe-side no-op when /refund already reversed) and must NOT debit."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_post_payout",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_2", "refunded": True}},
    }
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "listing_id": "l1",
                    "seller_id": "s1",
                    "price_paid_cents": 2000,
                    "seller_share_cents": 1700,
                    "seller_transfer_id": "tr_xyz",  # already paid out
                    "payout_accounted": True,  # flush booked the totals
                    "license_key_revoked": False,
                }
            ]
        }
    )

    async def fake_revoke(*, purchase_id, buyer_id, reason):
        return None

    mock_revoke.side_effect = fake_revoke
    mock_pa_table.return_value.update_item = MagicMock()

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_refund_post_payout"}',
    )
    assert resp.status_code == 200, resp.text

    mock_revoke.assert_awaited_once()
    # Held balance NOT decremented — booking already settled it.
    mock_pa_table.return_value.update_item.assert_not_called()
    # Reversal re-issued under the shared idempotency key (Stripe no-op).
    mock_reversal.assert_called_once()
    assert mock_reversal.call_args.kwargs["idempotency_key"] == "reversal:tr_xyz"


@patch("routers.marketplace_purchases.stripe.Transfer.create_reversal")
@patch("routers.marketplace_purchases.stripe.Transfer.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.revoke")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_charge_refunded_dashboard_refund_reverses_seller_transfer(
    mock_pa_table,
    mock_purchases_table,
    mock_revoke,
    mock_dedup,
    mock_construct,
    mock_tr_retrieve,
    mock_reversal,
    client,
):
    """Regression: a refund issued from the STRIPE DASHBOARD (not our /refund
    route) on an already-transferred purchase arrives with no reversal —
    without one the buyer gets their money back, the seller keeps theirs,
    and the platform eats 100%. The webhook must claw back the transfer."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_dashboard",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_3", "refunded": True}},
    }
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "listing_id": "l1",
                    "seller_id": "s1",
                    "price_paid_cents": 2000,
                    "seller_share_cents": 1700,
                    "seller_transfer_id": "tr_abc",
                    "payout_accounted": True,  # flush booked the totals
                    "license_key_revoked": False,
                }
            ]
        }
    )
    mock_tr_retrieve.return_value = {"id": "tr_abc", "amount": 1700, "reversed": False}

    async def fake_revoke(*, purchase_id, buyer_id, reason):
        return None

    mock_revoke.side_effect = fake_revoke
    mock_pa_table.return_value.update_item = MagicMock()

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_refund_dashboard"}',
    )
    assert resp.status_code == 200, resp.text

    mock_reversal.assert_called_once()
    args, kwargs = mock_reversal.call_args
    assert args[0] == "tr_abc"
    assert kwargs["amount"] == 1700
    # Same idempotency key refund_purchase uses — whichever path runs first
    # wins and the other is a Stripe-side no-op.
    assert kwargs["idempotency_key"] == "reversal:tr_abc"
    # Held balance untouched (money already left it at flush time).
    mock_pa_table.return_value.update_item.assert_not_called()


@patch("routers.marketplace_purchases.license_service.revoke")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
def test_charge_refunded_partial_refund_is_noop(mock_dedup, mock_construct, mock_revoke, client):
    """charge.refunded fires on PARTIAL refunds too (refunded=False until the
    charge is fully refunded). Revoking the license / clawing back the whole
    seller share on a partial refund would punish a purchase the buyer still
    holds."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_partial",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_4", "refunded": False}},
    }

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_refund_partial"}',
    )
    assert resp.status_code == 200, resp.text
    mock_revoke.assert_not_called()


@patch("core.repositories.marketplace_repo._listings_table")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_account_updated_skips_refunded_purchases(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_listings_table, client
):
    """Regression: payout flush must skip refunded purchases. license_service
    .revoke leaves price_paid_cents intact (preserves buyer history), so
    without an explicit refund check the flush would still Transfer the
    seller for a purchase the buyer was already refunded for — direct
    accounting loss.
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_acct_refunded",
        "type": "account.updated",
        "data": {
            "object": {
                "id": "acct_1",
                "payouts_enabled": True,
                "metadata": {"seller_id": "s1"},
            }
        },
    }
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={
            "Item": {
                "seller_id": "s1",
                "balance_held_cents": 5000,
                "stripe_connect_account_id": "acct_1",
            }
        }
    )
    mock_pa_table.return_value.update_item = MagicMock()
    listings_table = MagicMock()
    listings_table.query = MagicMock(return_value={"Items": [{"listing_id": "l1", "seller_id": "s1"}]})
    mock_listings_table.return_value = listings_table
    # Two purchases: one paid, one refunded. The refunded one should be skipped.
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "price_paid_cents": 2000,
                    "stripe_transfer_group": "purchase_l1_b1",
                    "license_key_revoked": False,
                },
                {
                    "buyer_id": "b2",
                    "purchase_id": "p2",
                    "price_paid_cents": 3000,
                    "stripe_transfer_group": "purchase_l1_b2",
                    "license_key_revoked": True,  # refunded BEFORE seller onboarded
                    "license_key_revoked_reason": "refunded",
                },
            ]
        }
    )
    mock_purchases_table.return_value.update_item = MagicMock()
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})

    async def fake_transfer(*, connect_account_id, amount_cents, transfer_group):
        return f"tr_{transfer_group}"

    mock_transfer.side_effect = fake_transfer

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_acct_refunded"}',
    )
    assert resp.status_code == 200, resp.text

    # Only ONE Transfer should have been called — for the un-refunded purchase.
    assert mock_transfer.call_count == 1
    transfer_groups = [call.kwargs["transfer_group"] for call in mock_transfer.call_args_list]
    assert "purchase_l1_b1" in transfer_groups
    assert "purchase_l1_b2" not in transfer_groups
    # Held balance decremented by ONLY the un-refunded amount.
    pa_update = mock_pa_table.return_value.update_item.call_args.kwargs
    assert pa_update["ExpressionAttributeValues"][":t"] == 2000


@patch("core.repositories.marketplace_repo._listings_table")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_account_updated_skips_already_transferred_purchases_idempotently(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_listings_table, client
):
    """Regression: payout flush must skip purchases that another run already
    flagged. listing-created-index is eventually consistent — a replayed
    or concurrent account.updated can re-read the same purchase before
    seller_transfer_id is visible. Conditional update on
    attribute_not_exists(seller_transfer_id) is the source of truth;
    only successful updates bump transferred_total. Otherwise held balance
    decrements twice for one transfer.
    """
    from botocore.exceptions import ClientError

    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_acct_replay",
        "type": "account.updated",
        "data": {
            "object": {
                "id": "acct_1",
                "payouts_enabled": True,
                "metadata": {"seller_id": "s1"},
            }
        },
    }
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={
            "Item": {
                "seller_id": "s1",
                "balance_held_cents": 5000,
                "stripe_connect_account_id": "acct_1",
            }
        }
    )
    mock_pa_table.return_value.update_item = MagicMock()
    listings_table = MagicMock()
    listings_table.query = MagicMock(return_value={"Items": [{"listing_id": "l1", "seller_id": "s1"}]})
    mock_listings_table.return_value = listings_table
    # Two purchases. update_item succeeds for the first, raises
    # ConditionalCheckFailedException for the second (race-loser).
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "price_paid_cents": 2000,
                    "stripe_transfer_group": "purchase_l1_b1",
                },
                {
                    "buyer_id": "b2",
                    "purchase_id": "p2",
                    "price_paid_cents": 3000,
                    "stripe_transfer_group": "purchase_l1_b2",
                },
            ]
        }
    )
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})
    # Per purchase the settle path writes stamp then accounted-marker; the
    # race-loser's STAMP is the conditional failure (third call here).
    mock_purchases_table.return_value.update_item = MagicMock(
        side_effect=[
            None,  # p1 seller_transfer_id stamp
            None,  # p1 payout_accounted marker
            ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                "UpdateItem",
            ),  # p2 stamp — another run got there first
        ]
    )

    async def fake_transfer(*, connect_account_id, amount_cents, transfer_group):
        return f"tr_{transfer_group}"

    mock_transfer.side_effect = fake_transfer

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_acct_replay"}',
    )
    assert resp.status_code == 200, resp.text

    # Held balance was decremented by ONLY the first purchase's amount —
    # the second one race-lost the conditional and was skipped, even though
    # transfer_held_balance was called for it (Stripe idempotency catches).
    pa_update = mock_pa_table.return_value.update_item.call_args.kwargs
    assert pa_update["ExpressionAttributeValues"][":t"] == 2000


@patch("core.repositories.marketplace_repo._listings_table")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("core.repositories.marketplace_repo._purchases_table")
@patch("core.repositories.marketplace_repo._payout_accounts_table")
def test_account_updated_flushes_per_purchase_using_purchase_transfer_group(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_listings_table, client
):
    """Regression: payout flush must create one Transfer per purchase using
    each purchase's stored stripe_transfer_group. A single batched
    flush_{seller}_{ts} transfer would not be findable by
    stripe.Transfer.list at refund time, so refunds couldn't reverse the
    seller transfer (platform eats the cost).
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_acct_1",
        "type": "account.updated",
        "data": {
            "object": {
                "id": "acct_1",
                "payouts_enabled": True,
                "metadata": {"seller_id": "s1"},
            }
        },
    }
    # Payout account row with held balance.
    mock_pa_table.return_value.get_item = MagicMock(
        return_value={
            "Item": {
                "seller_id": "s1",
                "balance_held_cents": 5000,
                "stripe_connect_account_id": "acct_1",
            }
        }
    )
    mock_pa_table.return_value.update_item = MagicMock()
    # Listings table query (seller-created-index) returns one listing.
    listings_table = MagicMock()
    listings_table.query = MagicMock(return_value={"Items": [{"listing_id": "l1", "seller_id": "s1"}]})
    mock_listings_table.return_value = listings_table
    # Purchases table query (listing-created-index) returns two purchases.
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "price_paid_cents": 2000,
                    "stripe_transfer_group": "purchase_l1_b1_111",
                },
                {
                    "buyer_id": "b2",
                    "purchase_id": "p2",
                    "price_paid_cents": 3000,
                    "stripe_transfer_group": "purchase_l1_b2_222",
                },
            ]
        }
    )
    mock_purchases_table.return_value.update_item = MagicMock()
    mock_purchases_table.return_value.get_item = MagicMock(return_value={})

    async def fake_transfer(*, connect_account_id, amount_cents, transfer_group):
        return f"tr_{transfer_group}"

    mock_transfer.side_effect = fake_transfer

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_acct_1"}',
    )
    assert resp.status_code == 200, resp.text

    # One Transfer per purchase, each with the purchase's transfer_group.
    assert mock_transfer.call_count == 2
    transfer_groups = [call.kwargs["transfer_group"] for call in mock_transfer.call_args_list]
    assert "purchase_l1_b1_111" in transfer_groups
    assert "purchase_l1_b2_222" in transfer_groups

    # Each purchase row got its seller_transfer_id stamped AND its
    # payout_accounted marker (2 update_item calls per purchase).
    assert mock_purchases_table.return_value.update_item.call_count == 4

    # Per-purchase bookkeeping: one decrement per purchase, never zeroed.
    totals = [
        call.kwargs["ExpressionAttributeValues"][":t"] for call in mock_pa_table.return_value.update_item.call_args_list
    ]
    assert sorted(totals) == [2000, 3000]


@patch("routers.marketplace_purchases.webhook_dedup.delete_event")
@patch("routers.marketplace_purchases._handle_checkout_completed")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_rolls_back_dedup_on_handler_failure(mock_construct, mock_dedup, mock_handler, mock_delete, client):
    """Regression: a transient failure in the handler must NOT leave the
    dedup row behind, otherwise Stripe's retry sees ALREADY_SEEN and
    silently drops the event.
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {"object": {}},
    }

    async def boom(*_a, **_k):
        raise RuntimeError("transient DDB error")

    mock_handler.side_effect = boom

    async def del_ok(*_a, **_k):
        return None

    mock_delete.side_effect = del_ok

    # TestClient's default raise_server_exceptions=True re-raises the
    # handler's exception. Build a fresh client that returns 500 to the
    # test instead so we can assert on the rollback side-effect cleanly.
    from main import app

    raising_client = TestClient(app, raise_server_exceptions=False)
    resp = raising_client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b'{"id":"evt_1"}',
    )
    assert resp.status_code == 500
    mock_delete.assert_called_once_with(event_id="stripe-marketplace:evt_1")


# ---------------------------------------------------------------------------
# require_org_admin gate on checkout + refund
# ---------------------------------------------------------------------------


@pytest.fixture
def org_member_client():
    """TestClient authed as a non-admin org member.

    Buying or refunding spends from the shared org Stripe customer, so a
    non-admin member cannot initiate either action.
    """
    from main import app
    from core.auth import AuthContext, get_current_user

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_test",
        org_id="org_test",
        org_role="org:member",
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@patch("routers.marketplace_purchases.stripe.checkout.Session.create")
def test_checkout_non_admin_org_member_403(mock_stripe_create, org_member_client):
    """Non-admin org members can't create a Stripe Checkout session against
    the shared org account. Gate fires before any Stripe call.
    """
    with patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock) as mock_get:
        resp = org_member_client.post(
            "/api/v1/marketplace/checkout",
            json={
                "listing_id": "lst_1",
                "success_url": "https://example.com/ok",
                "cancel_url": "https://example.com/no",
            },
        )
    assert resp.status_code == 403
    mock_stripe_create.assert_not_called()
    mock_get.assert_not_called()


@patch("routers.marketplace_purchases.stripe.checkout.Session.create")
def test_checkout_already_entitled_409s_before_stripe(mock_stripe_create, client):
    """An entitled buyer must never reach Stripe again — the storefront's
    recovery path after a broken paid deploy used to re-prompt payment.
    Consumers branch on the structured detail and deploy instead."""
    from main import app

    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="human_b1", org_id="b1", org_role="org:admin"
    )
    try:
        with (
            patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock) as mock_get,
            patch(
                "routers.marketplace_purchases.marketplace_repo.has_unrevoked_purchase",
                new_callable=AsyncMock,
            ) as mock_owned,
        ):
            mock_get.return_value = {
                "listing_id": "lst_1",
                "status": "published",
                "price_cents": 2000,
                "name": "Pulse",
                "slug": "pulse",
                "version": 1,
                "seller_id": "s1",
            }
            mock_owned.return_value = True
            resp = client.post(
                "/api/v1/marketplace/checkout",
                json={
                    "listing_id": "lst_1",
                    "success_url": "https://isol8.co/marketplace/deploy-success",
                    "cancel_url": "https://isol8.co/marketplace/listing/lst_1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {"error": "already_entitled"}
    mock_stripe_create.assert_not_called()


@pytest.mark.parametrize("seller_id", ["human_s1", "org_s1"])
@patch("routers.marketplace_purchases.stripe.checkout.Session.create")
def test_checkout_refuses_the_listings_own_seller(mock_stripe_create, client, seller_id):
    """A seller must not be charged for their own listing (they deploy it free
    through the seller bypass). Listings carry either the publishing user's id
    or the workspace's id as ``seller_id``; both are the seller."""
    from main import app

    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="human_s1", org_id="org_s1", org_role="org:admin"
    )
    try:
        with (
            patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock) as mock_get,
            patch(
                "routers.marketplace_purchases.marketplace_repo.has_unrevoked_purchase",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            mock_get.return_value = {
                "listing_id": "lst_1",
                "status": "published",
                "price_cents": 2000,
                "name": "Pulse",
                "slug": "pulse",
                "version": 1,
                "seller_id": seller_id,
            }
            resp = client.post(
                "/api/v1/marketplace/checkout",
                json={
                    "listing_id": "lst_1",
                    "success_url": f"https://{RETURN_APEX}/marketplace/deploy-success",
                    "cancel_url": f"https://{RETURN_APEX}/marketplace/listing/lst_1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "This is your own listing — you can't buy it."
    mock_stripe_create.assert_not_called()


@patch("routers.marketplace_purchases.stripe.checkout.Session.create")
def test_checkout_scopes_purchase_identity_to_org(mock_stripe_create, client):
    """Regression (codex): the install is org-scoped, so purchase identity
    must be the RESOLVED owner. Keying to the individual admin let a second
    org admin be charged again for a listing the org already owns."""
    from main import app

    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="admin_b",
        org_id="org_test",
        org_role="org:admin",
    )
    try:
        with (
            patch("core.services.marketplace_service.get_published_by_id", new_callable=AsyncMock) as mock_get,
            patch(
                "routers.marketplace_purchases.marketplace_repo.has_unrevoked_purchase",
                new_callable=AsyncMock,
            ) as mock_owned,
        ):
            mock_get.return_value = {
                "listing_id": "lst_1",
                "status": "published",
                "price_cents": 2000,
                "name": "Pulse",
                "slug": "pulse",
                "version": 1,
                "seller_id": "s1",
            }
            # Admin A already bought for the org — admin B must be refused.
            mock_owned.return_value = True
            resp = client.post(
                "/api/v1/marketplace/checkout",
                json={
                    "listing_id": "lst_1",
                    "success_url": "https://isol8.co/marketplace/deploy-success",
                    "cancel_url": "https://isol8.co/marketplace/listing/lst_1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    mock_owned.assert_awaited_once_with(buyer_id="org_test", listing_id="lst_1")
    mock_stripe_create.assert_not_called()


@patch("routers.marketplace_purchases.payout_service.refund_purchase")
@patch("core.repositories.marketplace_repo._purchases_table")
def test_refund_non_admin_org_member_403(mock_table, mock_refund, org_member_client):
    """Non-admin org members can't initiate refunds. Gate fires before any
    DDB lookup or payout-service call.
    """
    resp = org_member_client.post("/api/v1/marketplace/refund?purchase_id=p_x")
    assert resp.status_code == 403
    mock_table.assert_not_called()
    mock_refund.assert_not_called()


class TestValidateReturnUrl:
    """Stripe Checkout return URLs are client-supplied; anything off an
    isol8.co origin is rejected before a session is ever created."""

    def _validate(self, url):
        from fastapi import HTTPException

        from routers.marketplace_purchases import _validate_return_url

        try:
            return _validate_return_url(url, "success_url"), None
        except HTTPException as e:
            return None, e

    def test_allows_isol8_origins(self):
        for url in (
            f"https://{RETURN_APEX}/deploy-success?session_id={{CHECKOUT_SESSION_ID}}",
            f"https://dev.{RETURN_APEX}/listing/l1",
            f"https://company.{RETURN_APEX}/deploy-success",
            "http://localhost:3000/deploy-success",  # local dev
        ):
            ok, err = self._validate(url)
            assert err is None, f"{url} should be allowed: {err and err.detail}"
            assert ok == url

    def test_rejects_foreign_and_lookalike_origins(self):
        for url in (
            "https://evil.com/phish",
            "https://isol8.co.evil.com/phish",  # suffix lookalike
            "https://notisol8.co/",
            "http://isol8.co/",  # https required off-localhost
            "javascript:alert(1)",
            "",
        ):
            ok, err = self._validate(url)
            assert err is not None and err.status_code == 400, f"{url} should be rejected"


@patch("routers.marketplace_purchases.license_service.revoke", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.payout_service.refund_purchase", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.marketplace_repo.get_purchase", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.time.time")
def test_refund_window_parses_created_at_as_utc_not_local(mock_time, mock_get, mock_refund, mock_revoke, client):
    """The 7-day refund window must read created_at ("...Z") as UTC.

    time.mktime would read the struct as LOCAL time, so on a non-UTC host the
    computed age is off by the host's UTC offset. Pin a non-UTC TZ and a
    purchase created 7d4h before "now": correct UTC math => expired (403);
    the old local-time math (LA, UTC-7/8) would shave hours off the age and
    wrongly allow the refund.
    """
    import calendar
    import time as _time

    from core.auth import AuthContext, get_current_user
    from main import app

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    _time.tzset()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="human_buyer1", org_id="buyer1", org_role="org:admin"
    )
    try:
        now = calendar.timegm(_time.strptime("2026-06-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        mock_time.return_value = now
        # 7 days + 4 hours before now, in UTC -> genuinely past the 7-day window.
        mock_get.return_value = {"created_at": "2026-05-24T20:00:00Z"}

        resp = client.post("/api/v1/marketplace/refund?purchase_id=p1")

        assert resp.status_code == 403, resp.text
        mock_refund.assert_not_called()
    finally:
        app.dependency_overrides.clear()
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()


# --- Connect money-webhooks -> rollup refresh ------------------------------


def _money_event(event_type: str, account: str | None = None) -> dict:
    ev = {"id": f"evt_{event_type}", "type": event_type, "data": {"object": {"id": "obj_1"}}}
    if account is not None:
        ev["account"] = account
    return ev


@patch("routers.marketplace_purchases.rollup_service.refresh_owner_day", new_callable=AsyncMock)
@patch(
    "routers.marketplace_purchases.billing_repo.get_by_company_stripe_account_id",
    new_callable=AsyncMock,
    return_value={"owner_id": "own_1"},
)
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_money_event_on_company_account_schedules_rollup_refresh(
    mock_construct, mock_dedup, mock_lookup, mock_refresh, client
):
    from datetime import datetime, timezone

    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = _money_event("payment_intent.succeeded", account="acct_company")

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b"{}",
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_lookup.assert_awaited_once_with("acct_company")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mock_refresh.assert_called_once_with("own_1", today)


@patch("routers.marketplace_purchases.rollup_service.refresh_owner_day", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_money_event_on_seller_account_acks_without_refresh(mock_construct, mock_dedup, mock_refresh, client):
    """A seller's connected account is not a company row (autouse fixture
    returns None) -> the event falls through to the ack, no rollup touch."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = _money_event("payout.paid", account="acct_seller")

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b"{}",
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_refresh.assert_not_called()


@patch("routers.marketplace_purchases.rollup_service.refresh_owner_day", new_callable=AsyncMock)
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_money_event_without_account_acked(mock_construct, mock_dedup, mock_refresh, client):
    """Platform-scoped delivery carries no `account` on the envelope -> ack."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = _money_event("customer.created")

    resp = client.post(
        "/api/v1/marketplace/webhooks/stripe-marketplace",
        headers={"stripe-signature": "test"},
        content=b"{}",
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_refresh.assert_not_called()
