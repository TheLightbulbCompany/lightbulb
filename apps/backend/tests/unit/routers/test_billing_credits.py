"""Tests for the credit endpoints — Metronome is the only balance source."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def webhook_dedup_table(monkeypatch):
    """Provision the moto-mocked webhook-dedup table.

    The Stripe webhook handler calls record_event_or_skip first; that helper
    fails fast if WEBHOOK_DEDUP_TABLE is empty, and the webhook tests below
    route through it. (The credits/credit-transactions tables died with the
    DDB ledger — Metronome holds the balance now.)
    """
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


def _fake_metronome_client(net_balance=12.34):
    fake = MagicMock()
    fake.get_net_balance = AsyncMock(return_value={"net_balance": net_balance})
    return fake


@pytest.mark.asyncio
async def test_get_balance_returns_metronome_balance(async_client, monkeypatch):
    """1234 cents (Metronome's USD credit type) -> 12_340_000 microcents."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(net_balance=1234)
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {
        "balance_microcents": 12_340_000,
        "balance_dollars": "12.34",
    }
    fake.get_net_balance.assert_awaited_once_with(customer_id="cust-uuid-1")


@pytest.mark.asyncio
async def test_get_balance_org_member_reads_owner_id(async_client_org_member, monkeypatch):
    """Org members and admin share one balance, keyed on owner_id (org_id).
    Without this, every org member sees no balance because their personal
    user_id has no Metronome customer — the bug that blocked org members at
    chat with code=out_of_credits in prod."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(net_balance=988)
    row_mock = AsyncMock(return_value={"owner_id": "org_test_456", "metronome_customer_id": "cust-org-1"})
    with (
        patch("routers.billing.billing_repo.get_by_owner_id", new=row_mock),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client_org_member.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json()["balance_microcents"] == 9_880_000
    row_mock.assert_awaited_once_with("org_test_456")


@pytest.mark.asyncio
async def test_top_up_requests_metronome_payment_gated_commit(async_client, webhook_dedup_table):
    """The top-up endpoint hands the request straight to
    metronome_funding.request_top_up — no Stripe Checkout session, no
    redirect URL. Metronome collects via the customer's saved card."""
    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(),
    ) as mock_request:
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "requested", "amount_cents": 2000}
    # Personal users have owner_id == user_id.
    mock_request.assert_awaited_once_with("user_test_123", 2000)


@pytest.mark.asyncio
async def test_top_up_funding_error_maps_to_400(async_client, webhook_dedup_table):
    from core.billing.metronome_funding import FundingError

    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(side_effect=FundingError("Metronome customer not provisioned for owner_id=user_test_123")),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 400
    assert "not provisioned" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_top_up_below_minimum_rejected(async_client, webhook_dedup_table):
    resp = await async_client.post(
        "/api/v1/billing/credits/top_up",
        json={"amount_cents": 100},
    )
    # 422 when Pydantic ge=500 catches it before the handler body runs.
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_auto_reload_endpoint_is_gone(async_client):
    """Auto-reload died with the DDB ledger — there is no off-session
    reload machinery against Metronome's payment-gated commits."""
    resp = await async_client.put(
        "/api/v1/billing/credits/auto_reload",
        json={"enabled": True, "threshold_cents": 500, "amount_cents": 5000},
    )
    assert resp.status_code == 404


def test_stripe_webhook_dispatch_has_no_checkout_session_handlers():
    """The Stripe-Checkout top-up flow is deleted: checkout.session.* events
    are no longer handled (the router acks unknown types with 200). Only the
    subscription lifecycle remains on the Stripe webhook."""
    from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS

    assert not any(t.startswith("checkout.session.") for t in STRIPE_WEBHOOK_HANDLERS)
    # Subscription lifecycle is untouched.
    assert "customer.subscription.created" in STRIPE_WEBHOOK_HANDLERS
    assert "customer.subscription.updated" in STRIPE_WEBHOOK_HANDLERS
    assert "customer.subscription.deleted" in STRIPE_WEBHOOK_HANDLERS


@pytest.mark.asyncio
async def test_checkout_session_completed_is_acked_without_processing(async_client, monkeypatch, webhook_dedup_table):
    """A stray checkout.session.completed (e.g. the trial-checkout flow's
    session) is acked 200 via the unknown-type default — no credit grant
    machinery exists anymore."""
    fake_event = {
        "id": "evt_cs_legacy_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_anything",
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
    resp = await async_client.post(
        "/api/v1/billing/webhooks/stripe",
        content=json.dumps(fake_event),
        headers={"stripe-signature": "ignored"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_top_up_org_admin_uses_org_owner_id(async_client_org_admin, webhook_dedup_table):
    """Org admin top-up funds the org's Metronome customer (owner_id =
    org_id) so every member draws from the shared balance."""
    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(),
    ) as mock_request:
        resp = await async_client_org_admin.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 5000},
        )
    assert resp.status_code == 200
    mock_request.assert_awaited_once_with("org_test_456", 5000)


@pytest.mark.asyncio
async def test_get_balance_null_when_lifecycle_disabled(async_client, monkeypatch):
    """With the lifecycle switch off, the balance degrades to null without
    constructing a Metronome client (no doomed calls against placeholder
    secrets) and without a billing-row read."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ) as row_mock,
        patch("core.billing.metronome_client.for_settings") as for_settings_mock,
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_microcents": None, "balance_dollars": None}
    for_settings_mock.assert_not_called()
    row_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_balance_metronome_error_yields_null(async_client, monkeypatch):
    """A Metronome outage must never 500 the balance endpoint — both fields
    degrade to null and the panel shows the unavailable state."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client()
    fake.get_net_balance = AsyncMock(side_effect=RuntimeError("metronome down"))
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_microcents": None, "balance_dollars": None}


@pytest.mark.asyncio
async def test_get_balance_no_metronome_customer_yields_null(async_client, monkeypatch):
    """Owners not yet provisioned in Metronome get null without any HTTP call."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client()
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "stripe_customer_id": "cus_x"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_microcents": None, "balance_dollars": None}
    fake.get_net_balance.assert_not_awaited()
