"""Tests for the credit-management endpoints."""

import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def credit_ledger_tables(monkeypatch):
    """Provision moto-mocked credits + credit-transactions + webhook-dedup tables.

    Plan 1 (Stripe webhook event dedup, merged in main as `c9113ca`) makes
    the Stripe webhook handler call record_event_or_skip first; that helper
    fails fast if WEBHOOK_DEDUP_TABLE is empty. The credit-top-up webhook
    test routes through that path, so the fixture also provisions the
    dedup table.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-webhook-event-dedup",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")
        yield


@pytest.mark.asyncio
async def test_get_balance_returns_microcents(async_client, credit_ledger_tables):
    with patch(
        "routers.billing.credit_ledger.get_balance",
        new=AsyncMock(return_value=12_345_678),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {
        "balance_microcents": 12_345_678,
        "balance_dollars": "12.35",
    }


@pytest.mark.asyncio
async def test_get_balance_org_member_reads_owner_id(async_client_org_member, credit_ledger_tables):
    """Org members and admin share one credit balance, keyed on owner_id (org_id).
    Without this, every org member sees balance=0 because their personal user_id
    has no credit row — the bug that blocked org members at chat with
    code=out_of_credits in prod."""
    captured: dict = {}

    async def fake_get_balance(key, *, consistent: bool = False):
        captured["key"] = key
        return 9_876_543

    with patch("routers.billing.credit_ledger.get_balance", new=fake_get_balance):
        resp = await async_client_org_member.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_microcents": 9_876_543, "balance_dollars": "9.88"}
    assert captured["key"] == "org_test_456"


@pytest.mark.asyncio
async def test_get_balance_personal_uses_user_id(async_client, credit_ledger_tables):
    """Personal users have owner_id == user_id, so the read is unchanged."""
    captured: dict = {}

    async def fake_get_balance(key, *, consistent: bool = False):
        captured["key"] = key
        return 1_000_000

    with patch("routers.billing.credit_ledger.get_balance", new=fake_get_balance):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert captured["key"] == "user_test_123"


@pytest.mark.asyncio
async def test_top_up_creates_checkout_session(async_client, credit_ledger_tables):
    """Top-up endpoint returns a Stripe Checkout URL (Elements-flow
    replacement). The session has allow_promotion_codes=True so an
    internal 100%-off coupon can be redeemed; metadata carries the
    credit-grant amount so the webhook handler knows what to write to
    the ledger regardless of what was charged."""
    fake_session = type("S", (), {"id": "cs_test", "url": "https://checkout.stripe.com/c/pay/cs_test"})()
    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ),
        patch("stripe.checkout.Session.create", return_value=fake_session) as mock_session,
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test"
    _, kwargs = mock_session.call_args
    assert kwargs["mode"] == "payment"
    assert kwargs["customer"] == "cus_test"
    assert kwargs["allow_promotion_codes"] is True
    line_item = kwargs["line_items"][0]
    assert line_item["price_data"]["unit_amount"] == 2000
    assert line_item["price_data"]["currency"] == "usd"
    assert kwargs["metadata"]["purpose"] == "credit_top_up"
    # Personal users have owner_id == user_id, so the value is unchanged.
    # Key was renamed user_id -> owner_id when credits became org-pooled.
    assert kwargs["metadata"]["owner_id"] == "user_test_123"
    assert "user_id" not in kwargs["metadata"]
    assert kwargs["metadata"]["amount_cents"] == "2000"
    assert "idempotency_key" in kwargs


@pytest.mark.asyncio
async def test_top_up_below_minimum_rejected(async_client, credit_ledger_tables):
    resp = await async_client.post(
        "/api/v1/billing/credits/top_up",
        json={"amount_cents": 100},
    )
    # 422 when Pydantic ge=500 catches it before the handler body runs.
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_set_auto_reload_persists(async_client, credit_ledger_tables):
    with patch("routers.billing.credit_ledger.set_auto_reload", new=AsyncMock()) as mock_set:
        resp = await async_client.put(
            "/api/v1/billing/credits/auto_reload",
            json={"enabled": True, "threshold_cents": 500, "amount_cents": 5000},
        )
    assert resp.status_code == 200
    _, kwargs = mock_set.call_args
    assert kwargs["enabled"] is True
    assert kwargs["threshold_cents"] == 500
    assert kwargs["amount_cents"] == 5000


@pytest.mark.asyncio
async def test_set_auto_reload_org_uses_owner_id(async_client_org_admin, credit_ledger_tables):
    """Auto-reload settings live on the same row as the balance — must be
    owner-keyed so they survive across org members."""
    captured: dict = {}

    async def fake_set(key, *, enabled, threshold_cents, amount_cents):
        captured["key"] = key

    with patch("routers.billing.credit_ledger.set_auto_reload", new=fake_set):
        resp = await async_client_org_admin.put(
            "/api/v1/billing/credits/auto_reload",
            json={"enabled": True, "threshold_cents": 500, "amount_cents": 5000},
        )
    assert resp.status_code == 200
    assert captured["key"] == "org_test_456"


@pytest.mark.asyncio
async def test_checkout_session_completed_credits_ledger(async_client, monkeypatch, credit_ledger_tables):
    """Charged top-up: amount_total matches metadata.amount_cents — full
    Stripe transaction, no discount applied."""
    fake_event = {
        "id": "evt_cs_credit_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test",
                "payment_status": "paid",
                "amount_total": 2000,
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": "u_buyer",
                    "amount_cents": "2000",
                },
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )

    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock(return_value=20_000_000)) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )

    assert resp.status_code == 200
    _, kwargs = mock_top_up.call_args
    assert kwargs["amount_microcents"] == 20_000_000
    assert kwargs["stripe_payment_intent_id"] == "cs_test"


@pytest.mark.asyncio
async def test_checkout_session_completed_with_full_discount_still_credits(
    async_client, monkeypatch, credit_ledger_tables
):
    """100%-off coupon: amount_total=0 (Stripe didn't charge) but the
    user still gets the requested credit balance. This is the internal-
    use case — the company absorbs the cost via the coupon, no Stripe
    fee, and the ledger reflects the credit grant."""
    fake_event = {
        "id": "evt_cs_credit_discount_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_internal",
                "payment_status": "no_payment_required",
                "amount_total": 0,
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": "u_internal",
                    "amount_cents": "5000",
                },
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )

    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock(return_value=50_000_000)) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )

    assert resp.status_code == 200
    _, kwargs = mock_top_up.call_args
    # Credit reflects the *requested* amount, not the (zero) Stripe charge.
    assert kwargs["amount_microcents"] == 50_000_000
    assert kwargs["stripe_payment_intent_id"] == "cs_internal"


@pytest.mark.asyncio
async def test_checkout_session_completed_ignores_non_credit_purposes(async_client, monkeypatch, credit_ledger_tables):
    """Trial-checkout sessions ALSO fire checkout.session.completed but
    they aren't credit grants — they're handled via subscription.created
    higher up. The handler must ignore non-credit_top_up sessions to
    avoid accidentally crediting trial signups."""
    fake_event = {
        "id": "evt_cs_subscription_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_subscription",
                "amount_total": 0,
                "metadata": {},  # trial-checkout sessions don't set purpose
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )
    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock()) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )
    assert resp.status_code == 200
    mock_top_up.assert_not_called()


@pytest.mark.asyncio
async def test_checkout_session_completed_with_unpaid_status_defers_credit(
    async_client, monkeypatch, credit_ledger_tables
):
    """ACH / bank-transfer / Klarna sessions land in
    checkout.session.completed with payment_status=unpaid. We must NOT
    credit at this point — wait for async_payment_succeeded."""
    fake_event = {
        "id": "evt_cs_unpaid_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_ach_pending",
                "payment_status": "unpaid",
                "amount_total": 5000,
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": "u_ach",
                    "amount_cents": "5000",
                },
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )
    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock()) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )
    assert resp.status_code == 200
    mock_top_up.assert_not_called()


@pytest.mark.asyncio
async def test_async_payment_succeeded_credits_after_ach_settles(async_client, monkeypatch, credit_ledger_tables):
    """When ACH/bank-transfer settles, Stripe fires
    checkout.session.async_payment_succeeded with payment_status=paid.
    THIS is the event that credits the ledger for delayed payments."""
    fake_event = {
        "id": "evt_cs_async_paid_1",
        "type": "checkout.session.async_payment_succeeded",
        "data": {
            "object": {
                "id": "cs_ach_settled",
                "payment_status": "paid",
                "amount_total": 5000,
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": "u_ach",
                    "amount_cents": "5000",
                },
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )
    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock(return_value=50_000_000)) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )
    assert resp.status_code == 200
    _, kwargs = mock_top_up.call_args
    assert kwargs["amount_microcents"] == 50_000_000
    assert kwargs["stripe_payment_intent_id"] == "cs_ach_settled"


@pytest.mark.asyncio
async def test_async_payment_failed_does_not_credit(async_client, monkeypatch, credit_ledger_tables):
    """ACH bounce / bank-transfer reject. Because we never credited on
    the original completed-but-unpaid event, there's nothing to roll
    back — just emit a metric."""
    fake_event = {
        "id": "evt_cs_async_failed_1",
        "type": "checkout.session.async_payment_failed",
        "data": {
            "object": {
                "id": "cs_ach_failed",
                "payment_status": "unpaid",
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": "u_ach",
                    "amount_cents": "5000",
                },
            }
        },
    }
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda body, sig, secret: fake_event,
    )
    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock()) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )
    assert resp.status_code == 200
    mock_top_up.assert_not_called()


@pytest.mark.asyncio
async def test_top_up_org_admin_writes_org_id_to_metadata(async_client_org_admin, credit_ledger_tables):
    """Org admin top-up: charges the org's Stripe customer, but the credit
    grant is written to the org_id row so every member of the org sees the
    balance. Without this, the admin pays but only the admin sees credits."""
    fake_session = type("S", (), {"id": "cs_test_org", "url": "https://checkout.stripe.com/c/pay/cs_test_org"})()
    with (
        patch(
            "core.services.billing_service.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test_org"}),
        ),
        patch("stripe.checkout.Session.create", return_value=fake_session) as mock_session,
    ):
        resp = await async_client_org_admin.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 5000},
        )
    assert resp.status_code == 200
    _, kwargs = mock_session.call_args
    assert kwargs["customer"] == "cus_test_org"
    assert kwargs["metadata"]["owner_id"] == "org_test_456"
    assert kwargs["payment_intent_data"]["metadata"]["owner_id"] == "org_test_456"
    assert "user_id" not in kwargs["metadata"]


@pytest.mark.asyncio
async def test_webhook_metadata_without_owner_id_no_ops(async_client, monkeypatch, credit_ledger_tables):
    """No legacy fallback. A Checkout session whose metadata is missing
    owner_id is an error — log + no-op. We only accept the strict
    post-cutover shape since there are no in-flight legacy sessions."""
    fake_event = {
        "id": "evt_no_owner_id",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_missing_owner",
                "customer": "cus_test",
                "payment_status": "paid",
                "amount_total": 2000,
                "metadata": {
                    "purpose": "credit_top_up",
                    "user_id": "u_legacy",
                    "amount_cents": "2000",
                },
            }
        },
    }
    monkeypatch.setattr("stripe.Webhook.construct_event", lambda body, sig, secret: fake_event)
    with patch("routers.billing.credit_ledger.top_up", new=AsyncMock()) as mock_top_up:
        resp = await async_client.post(
            "/api/v1/billing/webhooks/stripe",
            content=json.dumps(fake_event),
            headers={"stripe-signature": "ignored"},
        )
    assert resp.status_code == 200
    mock_top_up.assert_not_called()
