"""Default-payment-method promotion in the subscription webhook handlers.

Stripe Checkout attaches the collected payment method to the SUBSCRIPTION
only; the Customer's invoice_settings.default_payment_method stays empty.
Metronome's charge_automatically collection charges the CUSTOMER default,
so handle_subscription_state must promote the subscription's PM to the
customer default — idempotently, and without ever failing the webhook.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import stripe_webhook_handlers as handlers  # noqa: E402
from core.services.webhook_dedup import WebhookDedupResult  # noqa: E402

RECORDED_RESULT = WebhookDedupResult.RECORDED
ALREADY_SEEN_RESULT = WebhookDedupResult.ALREADY_SEEN


@pytest.fixture(autouse=True)
def facts_mock(monkeypatch):
    """handle_subscription_state refreshes the owner-facts file after the
    status persist. Mock it for every test here (it would otherwise reach
    for the real billing repo + EFS workspace) and expose it for the tests
    that assert on the refresh."""
    mock = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", mock)
    return mock


def _subscription_event(**overrides) -> dict:
    event_data = {
        "id": "sub_x",
        "customer": "cus_x",
        "status": "trialing",
        "trial_end": 1700000000,
        "metadata": {"owner_id": "org_x"},
        "default_payment_method": "pm_x",
        "items": {
            "data": [
                {
                    "id": "si_workspace",
                    "price": {"id": "price_workspace_test", "recurring": {"interval": "month"}},
                }
            ]
        },
    }
    event_data.update(overrides)
    return {"id": "evt_x", "type": "customer.subscription.created", "data": {"object": event_data}}


@pytest.fixture
def mock_repo():
    with patch.object(handlers, "billing_repo") as repo:
        repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "org_x", "owner_type": "org", "stripe_customer_id": "cus_x"}
        )
        repo.set_subscription = AsyncMock()
        repo.set_provider_choice = AsyncMock()
        # Every valid event reaches the plan branch, so this must be async.
        repo.set_plan = AsyncMock()
        yield repo


@pytest.fixture(autouse=True)
def reconcile_dispatch(monkeypatch):
    """Every runtime-affecting billing change dispatches a durable reconcile execution. Stub the
    dispatcher for every test here so no handler path reaches Step Functions; the tests below
    assert on this mock."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    return dispatch


@pytest.mark.asyncio
async def test_sets_customer_default_pm_when_empty(mock_repo):
    """Sub carries a PM + customer default is empty → promote it."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": None}}

        result = await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    assert result is None
    mock_stripe.Customer.retrieve.assert_called_once_with("cus_x")
    mock_stripe.Customer.modify.assert_called_once_with("cus_x", invoice_settings={"default_payment_method": "pm_x"})
    assert "billing.default_pm.set" in [c.args[0] for c in mock_metric.call_args_list]


@pytest.mark.asyncio
async def test_skips_when_customer_already_has_default(mock_repo):
    """An existing customer default is never clobbered (idempotent on
    .updated re-deliveries and on PM changes the user made deliberately)."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm_existing"}}

        await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    mock_stripe.Customer.modify.assert_not_called()
    metric_names = [c.args[0] for c in mock_metric.call_args_list]
    assert "billing.default_pm.set" not in metric_names
    assert "billing.default_pm.error" not in metric_names


@pytest.mark.asyncio
async def test_skips_when_subscription_carries_no_pm(mock_repo):
    """No default_payment_method on the subscription → no Stripe calls at
    all (the common .updated case for status-only transitions)."""
    with patch.object(handlers, "stripe") as mock_stripe:
        await handlers.handle_subscription_state(_subscription_event(default_payment_method=None), MagicMock())

    mock_stripe.Customer.retrieve.assert_not_called()
    mock_stripe.Customer.modify.assert_not_called()


@pytest.mark.asyncio
async def test_expanded_pm_object_uses_its_id(mock_repo):
    """Events built with expansions carry the PM as an object; use its id."""
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {}}

        await handlers.handle_subscription_state(
            _subscription_event(default_payment_method={"id": "pm_expanded"}), MagicMock()
        )

    mock_stripe.Customer.modify.assert_called_once_with(
        "cus_x", invoice_settings={"default_payment_method": "pm_expanded"}
    )


@pytest.mark.asyncio
async def test_stripe_error_does_not_fail_handler(mock_repo):
    """Best-effort: a Stripe failure emits the error metric and the handler
    still returns normally (Stripe must get a 200, not a retry storm)."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.side_effect = Exception("stripe down")

        result = await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    assert result is None
    assert "billing.default_pm.error" in [c.args[0] for c in mock_metric.call_args_list]


# ---------------------------------------------------------------------------
# conversion telemetry: status changes vs. the first invoice paid at list (T23)
# ---------------------------------------------------------------------------


def _invoice(**kw):
    base = {
        "id": "in_1",
        "customer": "cus_1",
        "amount_paid": 5000,
        "currency": "usd",
        "billing_reason": "subscription_cycle",
        "discounts": [],
        "parent": {"subscription_details": {"subscription": "sub_1", "metadata": {"workspace_id": "org_x"}}},
    }
    return {"data": {"object": {**base, **kw}}}


@pytest.mark.asyncio
async def test_invoice_paid_at_list_emits_invoice_paid_and_trial_converted_once():
    account = {"owner_id": "org_x", "plan": "standard", "stripe_customer_id": "cus_1"}
    claim = AsyncMock(side_effect=[RECORDED_RESULT, ALREADY_SEEN_RESULT])
    with (
        patch.object(handlers, "resolve_owner_account", AsyncMock(return_value=account)),
        patch.object(handlers, "capture_product_event") as capture,
        patch("core.services.webhook_dedup.record_event_or_skip", claim),
    ):
        await handlers.handle_invoice_paid(_invoice(), MagicMock())
        await handlers.handle_invoice_paid(_invoice(id="in_2"), MagicMock())
    events = [c.kwargs["event"] for c in capture.call_args_list]
    assert events == ["invoice_paid", "trial_converted", "invoice_paid"]
    paid = capture.call_args_list[0].kwargs["properties"]
    assert paid["at_list"] is True and paid["workspace_id"] == "org_x" and paid["plan"] == "standard"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoice",
    [
        {"discounts": ["di_1"]},
        {"amount_paid": 0},
        {"total_discount_amounts": [{"amount": 5000, "discount": "di_1"}]},
    ],
)
async def test_a_coupon_or_free_invoice_is_not_at_list_and_converts_nothing(invoice):
    account = {"owner_id": "org_x", "plan": "standard", "stripe_customer_id": "cus_1"}
    with (
        patch.object(handlers, "resolve_owner_account", AsyncMock(return_value=account)),
        patch.object(handlers, "capture_product_event") as capture,
    ):
        await handlers.handle_invoice_paid(_invoice(**invoice), MagicMock())
    assert [c.kwargs["event"] for c in capture.call_args_list] == ["invoice_paid"]
    assert capture.call_args.kwargs["properties"]["at_list"] is False


@pytest.mark.asyncio
async def test_a_paid_invoice_without_a_subscription_converts_nothing():
    account = {"owner_id": "org_x", "plan": "standard", "stripe_customer_id": "cus_1"}
    claim = AsyncMock(return_value=RECORDED_RESULT)
    with (
        patch.object(handlers, "resolve_owner_account", AsyncMock(return_value=account)),
        patch.object(handlers, "capture_product_event") as capture,
        patch("core.services.webhook_dedup.record_event_or_skip", claim),
    ):
        await handlers.handle_invoice_paid(_invoice(parent=None, billing_reason="manual"), MagicMock())
    assert [c.kwargs["event"] for c in capture.call_args_list] == ["invoice_paid"]
    claim.assert_not_awaited()


def test_invoice_paid_is_handled():
    assert handlers.STRIPE_WEBHOOK_HANDLERS["invoice.paid"] is handlers.handle_invoice_paid


@pytest.mark.asyncio
async def test_subscription_deleted_refreshes_owner_facts(facts_mock):
    """Immediate cancellation must rewrite the facts file with the canceled
    status BEFORE (best-effort) teardown — a surviving container's agents
    must not keep reporting an active paid plan."""
    billing_service = MagicMock()
    billing_service.cancel_subscription = AsyncMock()
    event = {
        "id": "evt_del",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_x", "customer": "cus_x", "metadata": {"owner_id": "org_x"}}},
    }
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch("core.services.hermes_provisioning.delete_workspace_runtime", AsyncMock()),
        # Teardown re-reads the row through core.services.entitlement_teardown
        # (it must: the caller's snapshot predates the cancellation write, and
        # the shared status is derived from BOTH providers). No Apple status
        # here, so the owner is unentitled and teardown proceeds.
        patch(
            "core.services.entitlement_teardown.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "canceled"}),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "org_x", "owner_type": "org", "stripe_customer_id": "cus_x"}
        )
        repo.set_plan = AsyncMock()
        await handlers.handle_subscription_deleted(event, billing_service)

    facts_mock.assert_awaited_once_with("org_x", account_overrides={"subscription_status": "canceled"})


@pytest.mark.asyncio
async def test_subscription_deleted_resets_plan_to_standard(facts_mock):
    """Cancellation must reset the pricing tier: teardown removes the container
    but leaves the billing row, so a lingering plan="ultra_pro" would misreport
    the tier and turn a later flat re-subscribe's first webhook into a bogus
    downgrade. set_plan(owner, "standard") is called, best-effort."""
    billing_service = MagicMock()
    billing_service.cancel_subscription = AsyncMock()
    event = {
        "id": "evt_del2",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_u", "customer": "cus_u", "metadata": {"owner_id": "org_u"}}},
    }
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch("core.services.hermes_provisioning.delete_workspace_runtime", AsyncMock()),
        # Teardown re-reads the row through core.services.entitlement_teardown
        # (it must: the caller's snapshot predates the cancellation write, and
        # the shared status is derived from BOTH providers). No Apple status
        # here, so the owner is unentitled and teardown proceeds.
        patch(
            "core.services.entitlement_teardown.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "canceled"}),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "org_u",
                "owner_type": "org",
                "stripe_customer_id": "cus_u",
                "plan": "ultra_pro",
            }
        )
        repo.set_plan = AsyncMock()
        await handlers.handle_subscription_deleted(event, billing_service)

    repo.set_plan.assert_awaited_once()
    assert repo.set_plan.await_args.args[0] == "org_u"
    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
