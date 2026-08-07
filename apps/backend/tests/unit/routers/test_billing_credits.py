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


@pytest.fixture(autouse=True)
def one_click_consent():
    """POST /credits/top_up requires the billing row's credits_auto_charge
    consent (granted only via a prior opt-in Checkout). Most tests here
    exercise the paths BEHIND that gate, so consent defaults to granted;
    gate-specific and balance tests patch the row themselves — an inner
    ``with patch`` wins over this fixture for the duration of the test."""
    with patch(
        "routers.billing.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "credits_auto_charge": True,
                "stripe_customer_id": "cus_test_1",
                # Checkout sessions are refused unless the completion webhook
                # could actually grant — the row must look Metronome-ready.
                "metronome_customer_id": "mc_test_1",
                "metronome_contract_id": "mctr_test_1",
            }
        ),
    ):
        yield


def _fake_metronome_client(balance=12.34):
    # Live-verified getNetBalance shape (sandbox, 2026-06-10): the cents
    # number rides under "balance" in the data sub-object the client returns.
    fake = MagicMock()
    fake.get_net_balance = AsyncMock(return_value={"balance": balance, "credit_type_id": "ct-usd-cents"})
    return fake


@pytest.mark.asyncio
async def test_get_balance_returns_metronome_balance(async_client, monkeypatch):
    """1234 cents (Metronome's USD credit type) -> 12_340_000 microcents."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(balance=1234)
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
        patch("core.services.metronome_gate.apply_fresh_balance", new=AsyncMock()) as write_through,
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {
        "balance_microcents": 12_340_000,
        "balance_dollars": "12.34",
        "lifecycle_enabled": True,
        "auto_charge_enabled": False,
        # 12_340_000 is well above RESERVE_ESTIMATE_MICROCENTS (300_000) —
        # the gate would admit a request against this balance.
        "is_exhausted": False,
    }
    fake.get_net_balance.assert_awaited_once_with(customer_id="cust-uuid-1")
    # The successful live read is written through the fast-gate projection so
    # the provision gate un-blocks immediately instead of after the staleness
    # window.
    write_through.assert_awaited_once_with("user_test_123", 12_340_000)


@pytest.mark.asyncio
async def test_get_balance_write_through_restores_revoked_owner(async_client, monkeypatch):
    """End-to-end through the real apply_fresh_balance: a revoked owner whose
    live read (and confirming re-read) comes back positive gets the fast-gate
    row rewritten AND the Bedrock IAM grant restored from the balance poll."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(balance=1234)
    restore = AsyncMock()
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
        patch("core.services.metronome_gate.MetronomeFastGate") as gate_cls,
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "bedrock_revoked": True}),
        ),
        patch("core.billing.bedrock_aip.restore_owner_bedrock", new=restore),
    ):
        gate_cls.return_value.set_balance = AsyncMock()
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    gate_cls.return_value.set_balance.assert_awaited_once_with("user_test_123", remaining_microcents=12_340_000)
    restore.assert_awaited_once_with("user_test_123")


@pytest.mark.asyncio
async def test_get_balance_org_member_reads_owner_id(async_client_org_member, monkeypatch):
    """Org members and admin share one balance, keyed on owner_id (org_id).
    Without this, every org member sees no balance because their personal
    user_id has no Metronome customer — the bug that blocked org members at
    chat with code=out_of_credits in prod."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    fake = _fake_metronome_client(balance=988)
    row_mock = AsyncMock(return_value={"owner_id": "org_test_456", "metronome_customer_id": "cust-org-1"})
    with (
        patch("routers.billing.billing_repo.get_by_owner_id", new=row_mock),
        patch("core.billing.metronome_client.for_settings", return_value=fake),
        patch("core.services.metronome_gate.apply_fresh_balance", new=AsyncMock()),
    ):
        resp = await async_client_org_member.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json()["balance_microcents"] == 9_880_000
    # Read for the balance path AND for the consent flag — both keyed on the
    # ORG owner_id, never the member's personal user_id.
    assert all(call.args == ("org_test_456",) for call in row_mock.await_args_list)


@pytest.mark.asyncio
async def test_top_up_requests_metronome_payment_gated_commit(async_client):
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
    # Personal users have owner_id == user_id. No request_id in the body ⇒
    # no key from the endpoint (the funding layer falls back).
    mock_request.assert_awaited_once_with("user_test_123", 2000, idempotency_key=None)


@pytest.mark.asyncio
async def test_top_up_refreshes_gate_after_success(async_client, monkeypatch):
    """After a successful top-up the endpoint pulls the fresh balance through
    the gate (read + apply_fresh_balance) so a depleted+revoked owner's
    just-bought credits restore Bedrock immediately instead of racing the
    first post-top-up send (which otherwise fails AccessDenied)."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    read_balance = AsyncMock(return_value=20_000_000)
    apply_fresh = AsyncMock()
    with (
        patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()),
        patch("core.services.metronome_gate.read_net_balance_microcents", new=read_balance),
        patch("core.services.metronome_gate.apply_fresh_balance", new=apply_fresh),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    read_balance.assert_awaited_once_with("user_test_123")
    apply_fresh.assert_awaited_once_with("user_test_123", 20_000_000)


@pytest.mark.asyncio
async def test_top_up_refresh_skipped_when_no_balance(async_client, monkeypatch):
    """A None balance read (owner has no Metronome customer / outage) means
    nothing to apply — the endpoint must not call apply_fresh_balance with
    None."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    apply_fresh = AsyncMock()
    with (
        patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()),
        patch("core.services.metronome_gate.read_net_balance_microcents", new=AsyncMock(return_value=None)),
        patch("core.services.metronome_gate.apply_fresh_balance", new=apply_fresh),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    apply_fresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unreleased_balance", [0, -5_000])
async def test_top_up_does_not_revoke_on_unreleased_balance(async_client, monkeypatch, unreleased_balance):
    """A ≤0 read right after the commit is the gated commit not yet released
    (async Stripe collection), NOT a depleted owner. The top-up path must NOT
    feed it to apply_fresh_balance, which would RE-REVOKE someone who just
    paid — restore-only here, the lazy refresh handles the unreleased case."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    apply_fresh = AsyncMock()
    with (
        patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()),
        patch(
            "core.services.metronome_gate.read_net_balance_microcents",
            new=AsyncMock(return_value=unreleased_balance),
        ),
        patch("core.services.metronome_gate.apply_fresh_balance", new=apply_fresh),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    apply_fresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_top_up_refresh_failure_does_not_break_response(async_client, monkeypatch):
    """The post-top-up refresh is best-effort: a getNetBalance blip must not
    turn a SUCCESSFUL purchase into an error response."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    with (
        patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()),
        patch(
            "core.services.metronome_gate.read_net_balance_microcents",
            new=AsyncMock(side_effect=RuntimeError("getNetBalance blip")),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"


@pytest.mark.asyncio
async def test_top_up_skips_refresh_when_lifecycle_disabled(async_client, monkeypatch):
    """Lifecycle off ⇒ the endpoint must not attempt a Metronome balance read
    (mirrors the same guard the balance endpoint uses)."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    read_balance = AsyncMock(return_value=0)
    with (
        patch("core.billing.metronome_funding.request_top_up", new=AsyncMock()),
        patch("core.services.metronome_gate.read_net_balance_microcents", new=read_balance),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    read_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_top_up_namespaces_client_request_id_with_owner(async_client):
    """The client's request_id is attacker-chosen, so the endpoint prefixes
    it with the authenticated owner id before it becomes a Metronome
    Idempotency-Key — one user must never be able to replay or collide
    another user's in-flight commit."""
    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(),
    ) as mock_request:
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000, "request_id": "9d1f4c2e-uuid"},
        )
    assert resp.status_code == 200
    mock_request.assert_awaited_once_with("user_test_123", 2000, idempotency_key="topup-user_test_123-9d1f4c2e-uuid")


@pytest.mark.asyncio
async def test_top_up_overlong_request_id_rejected(async_client):
    """max_length=64 on the schema — an unbounded client token would flow
    into Metronome headers verbatim."""
    resp = await async_client.post(
        "/api/v1/billing/credits/top_up",
        json={"amount_cents": 2000, "request_id": "x" * 65},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_top_up_funding_error_maps_to_400(async_client):
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
async def test_top_up_below_minimum_rejected(async_client):
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


def test_stripe_webhook_dispatch_handles_checkout_sessions():
    """The explicit-Checkout top-up flow is back as the DEFAULT purchase
    path: checkout.session.completed + async_payment_succeeded (delayed
    methods like ACH) both dispatch to the credit-grant handler, alongside
    the untouched subscription lifecycle."""
    from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS, handle_checkout_session

    assert STRIPE_WEBHOOK_HANDLERS["checkout.session.completed"] is handle_checkout_session
    assert STRIPE_WEBHOOK_HANDLERS["checkout.session.async_payment_succeeded"] is handle_checkout_session
    assert "customer.subscription.created" in STRIPE_WEBHOOK_HANDLERS
    assert "customer.subscription.updated" in STRIPE_WEBHOOK_HANDLERS
    assert "customer.subscription.deleted" in STRIPE_WEBHOOK_HANDLERS


def _checkout_event(payment_status="paid", metadata=None, event_type="checkout.session.completed"):
    return {
        "id": "evt_cs_1",
        "type": event_type,
        "data": {
            "object": {
                "id": "cs_test_abc",
                "payment_status": payment_status,
                "amount_total": 2000,
                "metadata": (
                    metadata
                    if metadata is not None
                    else {
                        "purpose": "credit_top_up",
                        "owner_id": "u_buyer",
                        "amount_cents": "2000",
                        "auto_charge_opt_in": "false",
                    }
                ),
            }
        },
    }


async def _post_checkout_event(async_client, monkeypatch, event):
    monkeypatch.setattr("stripe.Webhook.construct_event", lambda body, sig, secret: event)
    return await async_client.post(
        "/api/v1/billing/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "ignored"},
    )


@pytest.mark.asyncio
async def test_checkout_session_paid_grants_ungated_credits(async_client, monkeypatch, webhook_dedup_table):
    """A paid credit-top-up session grants the credits as an UNGATED
    Metronome commit, keyed on the session id so Stripe redelivery can
    never double-grant."""
    grant = AsyncMock()
    with (
        patch("core.billing.metronome_funding.grant_prepaid_credits", new=grant),
        patch("routers.billing.billing_repo.set_credits_auto_charge", new=AsyncMock()) as set_flag,
    ):
        resp = await _post_checkout_event(async_client, monkeypatch, _checkout_event())
    assert resp.status_code == 200
    grant.assert_awaited_once_with("u_buyer", 2000, idempotency_key="checkout-cs_test_abc")
    # No opt-in in the metadata ⇒ the consent flag is untouched.
    set_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_session_opt_in_enables_auto_charge(async_client, monkeypatch, webhook_dedup_table):
    """auto_charge_opt_in=true on a PAID session flips the billing row's
    consent flag — the session that just succeeded carried the
    setup_future_usage consent, so this is the only legitimate enable path."""
    meta = {
        "purpose": "credit_top_up",
        "owner_id": "u_buyer",
        "amount_cents": "2000",
        "auto_charge_opt_in": "true",
    }
    set_flag = AsyncMock()
    with (
        patch("core.billing.metronome_funding.grant_prepaid_credits", new=AsyncMock()),
        patch(
            "core.services.stripe_webhook_handlers.billing_repo.set_credits_auto_charge",
            new=set_flag,
        ),
    ):
        resp = await _post_checkout_event(async_client, monkeypatch, _checkout_event(metadata=meta))
    assert resp.status_code == 200
    set_flag.assert_awaited_once_with("u_buyer", enabled=True)


@pytest.mark.asyncio
async def test_checkout_session_unpaid_defers_grant(async_client, monkeypatch, webhook_dedup_table):
    """A delayed-payment session (ACH etc.) fires completed with
    payment_status=unpaid — NO grant until async_payment_succeeded, so a
    failed ACH never leaves free credits."""
    grant = AsyncMock()
    with patch("core.billing.metronome_funding.grant_prepaid_credits", new=grant):
        resp = await _post_checkout_event(async_client, monkeypatch, _checkout_event(payment_status="unpaid"))
    assert resp.status_code == 200
    grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_session_non_topup_purpose_is_acked(async_client, monkeypatch, webhook_dedup_table):
    """Trial-checkout sessions (no credit_top_up purpose) pass through
    untouched — their state comes from the subscription handlers."""
    grant = AsyncMock()
    with patch("core.billing.metronome_funding.grant_prepaid_credits", new=grant):
        resp = await _post_checkout_event(async_client, monkeypatch, _checkout_event(metadata={}))
    assert resp.status_code == 200
    grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_top_up_org_admin_uses_org_owner_id(async_client_org_admin):
    """Org admin top-up funds the org's Metronome customer (owner_id =
    org_id) so every member draws from the shared balance — and the
    idempotency namespace is the org, not the admin's personal user_id."""
    with patch(
        "core.billing.metronome_funding.request_top_up",
        new=AsyncMock(),
    ) as mock_request:
        resp = await async_client_org_admin.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 5000, "request_id": "intent-1"},
        )
    assert resp.status_code == 200
    mock_request.assert_awaited_once_with("org_test_456", 5000, idempotency_key="topup-org_test_456-intent-1")


@pytest.mark.asyncio
async def test_get_balance_null_when_lifecycle_disabled(async_client, monkeypatch):
    """With the lifecycle switch off, the balance degrades to null without
    constructing a Metronome client (no doomed calls against placeholder
    secrets). The billing row is still read — it carries the auto-charge
    consent flag the response exposes."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "metronome_customer_id": "cust-uuid-1"}),
        ),
        patch("core.billing.metronome_client.for_settings") as for_settings_mock,
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    # lifecycle_enabled lets the frontend distinguish "credits product not
    # wired in this environment" from a transient balance outage.
    assert resp.json() == {
        "balance_microcents": None,
        "balance_dollars": None,
        "lifecycle_enabled": False,
        "auto_charge_enabled": False,
        # A null balance is INDETERMINATE (lifecycle off here), never
        # exhausted — a false banner is worse than a late one.
        "is_exhausted": False,
    }
    for_settings_mock.assert_not_called()


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
    assert resp.json() == {
        "balance_microcents": None,
        "balance_dollars": None,
        "lifecycle_enabled": True,
        "auto_charge_enabled": False,
        # A null balance (Metronome outage here) is INDETERMINATE, never
        # exhausted — a false banner is worse than a late one.
        "is_exhausted": False,
    }


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
    assert resp.json() == {
        "balance_microcents": None,
        "balance_dollars": None,
        "lifecycle_enabled": True,
        "auto_charge_enabled": False,
        # A null balance (no Metronome customer here) is INDETERMINATE, never
        # exhausted — a false banner is worse than a late one.
        "is_exhausted": False,
    }
    fake.get_net_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_top_up_without_auto_charge_consent_is_409(async_client):
    """One-click top-ups charge the saved card with no confirmation step, so
    they require standing consent (credits_auto_charge on the billing row,
    granted only via an opt-in Checkout). Without it the endpoint answers
    409 auto_charge_not_enabled and never reaches the funding layer."""
    request = AsyncMock()
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123"}),
        ),
        patch("core.billing.metronome_funding.request_top_up", new=request),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "auto_charge_not_enabled"
    request.assert_not_awaited()


def _fake_checkout_session(url="https://checkout.stripe.com/c/pay/cs_test_xyz"):
    session = MagicMock()
    session.url = url
    return session


@pytest.mark.asyncio
async def test_top_up_checkout_creates_session(async_client, monkeypatch):
    """The default purchase path: a Stripe Checkout session for the amount,
    metadata carrying the grant inputs the webhook needs, and NO off-session
    card consent unless the customer opted in."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-test-1")
    create = MagicMock(return_value=_fake_checkout_session())
    with patch("core.services.billing_service.stripe.checkout.Session.create", new=create):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up_checkout",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 200
    assert resp.json() == {"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_xyz"}
    kwargs = create.call_args.kwargs
    assert kwargs["mode"] == "payment"
    assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 2000
    assert kwargs["metadata"] == {
        "purpose": "credit_top_up",
        "owner_id": "user_test_123",
        "amount_cents": "2000",
        "auto_charge_opt_in": "false",
    }
    # No opt-in ⇒ no off-session consent is captured.
    assert "setup_future_usage" not in kwargs["payment_intent_data"]


@pytest.mark.asyncio
async def test_top_up_checkout_opt_in_captures_off_session_consent(async_client, monkeypatch):
    """enable_auto_charge rides as setup_future_usage=off_session — Stripe's
    documented consent artifact for charging the saved card later — and as
    session metadata so the webhook only flips the flag after THIS payment
    succeeds."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-test-1")
    create = MagicMock(return_value=_fake_checkout_session())
    with patch("core.services.billing_service.stripe.checkout.Session.create", new=create):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up_checkout",
            json={"amount_cents": 5000, "enable_auto_charge": True},
        )
    assert resp.status_code == 200
    kwargs = create.call_args.kwargs
    assert kwargs["payment_intent_data"]["setup_future_usage"] == "off_session"
    assert kwargs["metadata"]["auto_charge_opt_in"] == "true"


@pytest.mark.asyncio
async def test_top_up_checkout_requires_lifecycle(async_client, monkeypatch):
    """Without the Metronome lifecycle the completion webhook has nowhere to
    grant the credits — refuse before taking money that would strand."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    resp = await async_client.post(
        "/api/v1/billing/credits/top_up_checkout",
        json={"amount_cents": 2000},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_auto_charge_disable(async_client):
    """The settings surface may only turn one-click OFF — re-enabling
    requires a fresh consenting Checkout, so there is no enable endpoint."""
    set_flag = AsyncMock()
    with patch("routers.billing.billing_repo.set_credits_auto_charge", new=set_flag):
        resp = await async_client.post("/api/v1/billing/credits/auto_charge/disable")
    assert resp.status_code == 200
    assert resp.json() == {"auto_charge_enabled": False}
    set_flag.assert_awaited_once_with("user_test_123", enabled=False)


@pytest.mark.asyncio
async def test_handler_failure_rolls_back_dedup_for_redelivery(async_client, monkeypatch, webhook_dedup_table):
    """If the grant raises AFTER the dedup row was recorded (Metronome outage
    right after Stripe collected), the row must be rolled back: otherwise
    Stripe's redelivery hits ALREADY_SEEN and is acked without processing —
    money collected, credits never granted, permanently. With the rollback,
    the redelivery re-attempts and the (session-id-keyed, idempotent) grant
    lands."""
    grant = AsyncMock(side_effect=[RuntimeError("metronome outage"), None])
    with patch("core.billing.metronome_funding.grant_prepaid_credits", new=grant):
        # First delivery: grant blows up -> 500 back to Stripe.
        with pytest.raises(RuntimeError):
            await _post_checkout_event(async_client, monkeypatch, _checkout_event())
        # Stripe redelivers the SAME event id: must NOT be dedup-skipped.
        resp = await _post_checkout_event(async_client, monkeypatch, _checkout_event())
    assert resp.status_code == 200
    assert grant.await_count == 2
    grant.assert_awaited_with("u_buyer", 2000, idempotency_key="checkout-cs_test_abc")


@pytest.mark.asyncio
async def test_top_up_checkout_refuses_unprovisioned_owner(async_client, monkeypatch):
    """Take money ONLY when the webhook can grant: an owner whose Metronome
    customer/contract hasn't been provisioned yet must get a 400 BEFORE a
    payable session exists — otherwise Stripe collects and the grant
    500s/retries until provisioning lands (collected money in limbo)."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-test-1")
    create = MagicMock()
    with (
        patch(
            "routers.billing.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"owner_id": "user_test_123", "stripe_customer_id": "cus_test_1"}),
        ),
        # The checkout now PROVISIONS on demand before asserting readiness (an
        # unprovisioned owner used to be told "try again in a moment" forever,
        # on the very screen the credits gate sends them to). Stub it to a
        # clean no-op so this test still exercises what it is about: the money
        # guard when the ids are STILL absent afterwards. A raising stub would
        # exercise the permanent-failure message instead — covered separately
        # in test_billing_service.py.
        patch(
            "core.billing.metronome_provisioning.ensure_metronome_customer",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.billing_service.stripe.checkout.Session.create", new=create),
    ):
        resp = await async_client.post(
            "/api/v1/billing/credits/top_up_checkout",
            json={"amount_cents": 2000},
        )
    assert resp.status_code == 400
    assert "ready" in resp.json()["detail"].lower()
    create.assert_not_called()
