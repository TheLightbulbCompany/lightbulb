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
    # metronome_balance_microcents is display-only and None here: the moto
    # environment has no billing row / Metronome customer for this owner.
    assert resp.json() == {
        "balance_microcents": 12_345_678,
        "balance_dollars": "12.35",
        "metronome_balance_microcents": None,
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
    assert resp.json() == {
        "balance_microcents": 9_876_543,
        "balance_dollars": "9.88",
        "metronome_balance_microcents": None,
    }
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
async def test_top_up_requests_metronome_payment_gated_commit(async_client, credit_ledger_tables):
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
async def test_top_up_funding_error_maps_to_400(async_client, credit_ledger_tables):
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
async def test_checkout_session_completed_is_acked_without_processing(async_client, monkeypatch, credit_ledger_tables):
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
async def test_top_up_org_admin_uses_org_owner_id(async_client_org_admin, credit_ledger_tables):
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


def _fake_metronome_client(net_balance=12.34):
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.get_net_balance = AsyncMock(return_value={"net_balance": net_balance})
    return fake


@pytest.mark.asyncio
async def test_get_balance_includes_metronome_balance_when_provisioned(async_client, credit_ledger_tables, monkeypatch):
    """When the owner has a Metronome customer, the display-only
    metronome_balance_microcents rides along (Metronome cents -> microcents,
    see core/billing/metronome_units.py)."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(net_balance=1234)  # cents == $12.34
    with (
        patch("routers.billing.credit_ledger.get_balance", new=AsyncMock(return_value=1_000_000)),
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metronome_balance_microcents"] == 12_340_000
    # The DDB-ledger number is untouched by the Metronome read.
    assert body["balance_microcents"] == 1_000_000
    fake.get_net_balance.assert_awaited_once_with(customer_id="cust-uuid-1")


@pytest.mark.asyncio
async def test_get_balance_metronome_none_when_lifecycle_disabled(async_client, credit_ledger_tables, monkeypatch):
    """With the lifecycle switch off, the display degrades to None without
    constructing a Metronome client (no doomed calls against placeholder
    secrets)."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    with (
        patch("routers.billing.credit_ledger.get_balance", new=AsyncMock(return_value=1_000_000)),
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ) as row_mock,
        patch("core.billing.metronome_client.for_settings") as for_settings_mock,
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json()["metronome_balance_microcents"] is None
    for_settings_mock.assert_not_called()
    row_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_balance_metronome_error_yields_none(async_client, credit_ledger_tables, monkeypatch):
    """A Metronome outage must never 500 the balance endpoint — the field
    degrades to None and the ledger number still serves."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client()
    fake.get_net_balance = AsyncMock(side_effect=RuntimeError("metronome down"))
    with (
        patch("routers.billing.credit_ledger.get_balance", new=AsyncMock(return_value=2_000_000)),
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metronome_balance_microcents"] is None
    assert body["balance_microcents"] == 2_000_000


@pytest.mark.asyncio
async def test_get_balance_no_metronome_customer_yields_none(async_client, credit_ledger_tables, monkeypatch):
    """Owners not yet provisioned in Metronome get None without any HTTP call."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client()
    with (
        patch("routers.billing.credit_ledger.get_balance", new=AsyncMock(return_value=3_000_000)),
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "stripe_customer_id": "cus_x"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json()["metronome_balance_microcents"] is None
    fake.get_net_balance.assert_not_awaited()
