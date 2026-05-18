"""Tests for marketplace_purchases router."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


@patch("routers.marketplace_purchases.boto3.resource")
@patch("routers.marketplace_purchases._purchases_table")
def test_my_purchases_sorts_by_created_at_not_uuid(mock_purchases_table, mock_boto, client):
    """Regression: /my-purchases must sort by created_at, not by purchase_id
    (UUID, the table's sort key). ScanIndexForward=False on the base table
    returns reverse-lexical UUID order, not newest-first (Codex P2 on
    PR #517, commit 977bf178).
    """
    from main import app

    from core.auth import AuthContext, get_current_user

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="b1")

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
    fake_resource = MagicMock()
    fake_resource.Table.return_value = listings_table
    mock_boto.return_value = fake_resource

    resp = client.get("/api/v1/marketplace/my-purchases")
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    # Newest first — created_at desc, NOT lexical UUID desc.
    assert items[0]["created_at"] == "2026-04-15T00:00:00Z"
    assert items[0]["license_key"] == "iml_b"
    assert items[1]["created_at"] == "2026-01-01T00:00:00Z"


@patch("routers.marketplace_purchases.boto3.client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_webhook_checkout_completed_grants_license(
    mock_gen, mock_dedup, mock_construct, mock_pi_retrieve, mock_boto_client, client
):
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_111"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
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
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_webhook_skips_fulfillment_for_unpaid_delayed_payment(
    mock_pa_table, mock_purchases_table, mock_gen, mock_dedup, mock_construct, client
):
    """Regression: Stripe emits checkout.session.completed with
    payment_status='unpaid' for delayed-payment methods (ACH, BNPL); the
    actual settlement comes later as async_payment_succeeded. The fulfillment
    path must gate on payment_status='paid' or buyers receive entitlement on
    payments that may later fail (Codex P1 on PR #517, commit 40b698f8).
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


@patch("routers.marketplace_purchases.boto3.client")
@patch("routers.marketplace_purchases.stripe.PaymentIntent.retrieve")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.generate", return_value="iml_test")
def test_webhook_async_payment_succeeded_grants_license(
    mock_gen, mock_dedup, mock_construct, mock_pi_retrieve, mock_boto_client, client
):
    mock_pi_retrieve.return_value = {"transfer_group": "purchase_l1_b1_async"}
    fake_ddb = MagicMock()
    fake_ddb.transact_write_items = MagicMock(return_value={})
    mock_boto_client.return_value = fake_ddb
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
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_charge_refunded_debits_held_balance_when_pre_payout(
    mock_pa_table, mock_purchases_table, mock_revoke, mock_dedup, mock_construct, client
):
    """Regression: charge.refunded must subtract from balance_held_cents
    when the purchase hasn't been transferred yet. Otherwise R11's
    skip-refunded-in-flush leaves the held balance permanently inflated
    (Codex P1 round 12, commit b6d66c05).
    """
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_pre_payout",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_1"}},
    }
    # Purchase row WITHOUT seller_transfer_id — refund happened before payout.
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


@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.license_service.revoke")
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_charge_refunded_skips_held_balance_debit_when_already_transferred(
    mock_pa_table, mock_purchases_table, mock_revoke, mock_dedup, mock_construct, client
):
    """Regression: when the purchase was already transferred to the seller
    (seller_transfer_id present), refund_purchase already issues a
    Transfer Reversal which separately moves the money. The held balance
    was already drained at flush time, so don't decrement again."""
    from core.services.webhook_dedup import WebhookDedupResult

    mock_dedup.return_value = WebhookDedupResult.RECORDED
    mock_construct.return_value = {
        "id": "evt_refund_post_payout",
        "type": "charge.refunded",
        "data": {"object": {"payment_intent": "pi_2"}},
    }
    mock_purchases_table.return_value.query = MagicMock(
        return_value={
            "Items": [
                {
                    "buyer_id": "b1",
                    "purchase_id": "p1",
                    "listing_id": "l1",
                    "seller_id": "s1",
                    "price_paid_cents": 2000,
                    "seller_transfer_id": "tr_xyz",  # already paid out
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
    # Held balance NOT decremented — Transfer Reversal handled the seller side.
    mock_pa_table.return_value.update_item.assert_not_called()


@patch("routers.marketplace_purchases.boto3.resource")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_account_updated_skips_refunded_purchases(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_boto, client
):
    """Regression: payout flush must skip refunded purchases. license_service
    .revoke leaves price_paid_cents intact (preserves buyer history), so
    without an explicit refund check the flush would still Transfer the
    seller for a purchase the buyer was already refunded for — direct
    accounting loss (Codex P1 round 11, commit 69c76f5e).
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
    fake_resource = MagicMock()
    fake_resource.Table.return_value = listings_table
    mock_boto.return_value = fake_resource
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


@patch("routers.marketplace_purchases.boto3.resource")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_account_updated_skips_already_transferred_purchases_idempotently(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_boto, client
):
    """Regression: payout flush must skip purchases that another run already
    flagged. listing-created-index is eventually consistent — a replayed
    or concurrent account.updated can re-read the same purchase before
    seller_transfer_id is visible. Conditional update on
    attribute_not_exists(seller_transfer_id) is the source of truth;
    only successful updates bump transferred_total. Otherwise held balance
    decrements twice for one transfer (Codex P1 round 9, commit 3bc1a2eb).
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
    fake_resource = MagicMock()
    fake_resource.Table.return_value = listings_table
    mock_boto.return_value = fake_resource
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
    mock_purchases_table.return_value.update_item = MagicMock(
        side_effect=[
            None,
            ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                "UpdateItem",
            ),
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


@patch("routers.marketplace_purchases.boto3.resource")
@patch("routers.marketplace_purchases.payout_service.transfer_held_balance")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases._purchases_table")
@patch("routers.marketplace_purchases._payout_accounts_table")
def test_account_updated_flushes_per_purchase_using_purchase_transfer_group(
    mock_pa_table, mock_purchases_table, mock_dedup, mock_construct, mock_transfer, mock_boto, client
):
    """Regression: payout flush must create one Transfer per purchase using
    each purchase's stored stripe_transfer_group. A single batched
    flush_{seller}_{ts} transfer would not be findable by
    stripe.Transfer.list at refund time, so refunds couldn't reverse the
    seller transfer (platform eats the cost) (Codex P1 on PR #517,
    commit ba89f60c).
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
    mock_resource = MagicMock()
    mock_resource.Table.return_value = listings_table
    mock_boto.return_value = mock_resource
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

    # Each purchase row got its seller_transfer_id stamped.
    assert mock_purchases_table.return_value.update_item.call_count == 2

    # Held balance decremented by the sum, not zeroed.
    pa_update = mock_pa_table.return_value.update_item.call_args.kwargs
    assert pa_update["ExpressionAttributeValues"][":t"] == 5000


@patch("routers.marketplace_purchases.webhook_dedup.delete_event")
@patch("routers.marketplace_purchases._handle_checkout_completed")
@patch("routers.marketplace_purchases.webhook_dedup.record_event_or_skip")
@patch("routers.marketplace_purchases.stripe.Webhook.construct_event")
def test_webhook_rolls_back_dedup_on_handler_failure(mock_construct, mock_dedup, mock_handler, mock_delete, client):
    """Regression: a transient failure in the handler must NOT leave the
    dedup row behind, otherwise Stripe's retry sees ALREADY_SEEN and
    silently drops the event (Codex P1 on PR #517, commit bee2fa1c).
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
    mock_delete.assert_called_once_with(event_id="evt_1")


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
        org_role="org:basic_member",
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
    with patch("core.services.marketplace_service.get_by_slug", new_callable=AsyncMock) as mock_get:
        resp = org_member_client.post(
            "/api/v1/marketplace/checkout",
            json={
                "listing_slug": "demo-agent",
                "success_url": "https://example.com/ok",
                "cancel_url": "https://example.com/no",
            },
        )
    assert resp.status_code == 403
    mock_stripe_create.assert_not_called()
    mock_get.assert_not_called()


@patch("routers.marketplace_purchases.payout_service.refund_purchase")
@patch("routers.marketplace_purchases._purchases_table")
def test_refund_non_admin_org_member_403(mock_table, mock_refund, org_member_client):
    """Non-admin org members can't initiate refunds. Gate fires before any
    DDB lookup or payout-service call.
    """
    resp = org_member_client.post("/api/v1/marketplace/refund?purchase_id=p_x")
    assert resp.status_code == 403
    mock_table.assert_not_called()
    mock_refund.assert_not_called()
