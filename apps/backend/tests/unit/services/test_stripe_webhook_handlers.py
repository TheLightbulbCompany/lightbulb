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
from core.services.text_lines import sync_front_desk as _REAL_SYNC  # noqa: E402
from core.services.webhook_dedup import WebhookDedupResult  # noqa: E402

RECORDED_RESULT = WebhookDedupResult.RECORDED
ALREADY_SEEN_RESULT = WebhookDedupResult.ALREADY_SEEN


@pytest.fixture(autouse=True)
def workspace_price_configured(monkeypatch):
    """Positive subscription events use the configured Workspace price."""
    monkeypatch.setattr("core.config.settings.STRIPE_WORKSPACE_PRICE_ID", "price_workspace_test")


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


@pytest.mark.parametrize(
    ("price_id", "expected"),
    [("price_base_test", "base"), ("price_workspace_test", "standard"), ("price_ultra_test", "ultra_pro")],
)
def test_subscription_item_maps_each_configured_plan(monkeypatch, price_id, expected):
    monkeypatch.setattr("core.config.settings.STRIPE_BASE_PRICE_ID", "price_base_test")
    monkeypatch.setattr("core.config.settings.STRIPE_ULTRA_PRICE_ID", "price_ultra_test")
    event = _subscription_event(items={"data": [{"price": {"id": price_id, "recurring": {"interval": "month"}}}]})
    assert handlers._workspace_plan_from_subscription_items(event["data"]["object"]) == expected


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


@pytest.fixture(autouse=True)
def front_desk_sync(monkeypatch):
    """The Front desk line sync buys a real number; no handler test may reach it unasked."""
    sync = AsyncMock(return_value="absent")
    monkeypatch.setattr("core.services.text_lines.sync_front_desk", sync)
    return sync


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
    # The state sync itself still ran.
    mock_repo.set_subscription.assert_awaited_once()


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
    still completes the subscription-state sync (Stripe must get a 200, not
    a retry storm)."""
    with patch.object(handlers, "stripe") as mock_stripe, patch.object(handlers, "put_metric") as mock_metric:
        mock_stripe.Customer.retrieve.side_effect = Exception("stripe down")

        result = await handlers.handle_subscription_state(_subscription_event(), MagicMock())

    assert result is None
    assert "billing.default_pm.error" in [c.args[0] for c in mock_metric.call_args_list]
    mock_repo.set_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_backup_write_never_records_connector(mock_repo):
    """The provider_choice backup write persists the CHOICE but never the human.

    Subscription metadata's ``clerk_user_id`` is unauthenticated snapshot
    data: recording it onto the row wedged workspaces permanently when the id
    dangled (no such Clerk user — the provision gate waits on a ghost's
    tokens while /start and the provider switch 403 every real human).
    The connector is recorded only by authenticated writers (synchronous
    trial-checkout, change-provider, poll-time claim), so the backup passes
    None and an unset row stays claimable by the first real admin's flow.
    """
    # default_payment_method=None keeps the PM-promotion path inert so this
    # test isolates the provider_choice backup write.
    event = _subscription_event(
        default_payment_method=None,
        metadata={
            "owner_id": "org_x",
            "provider_choice": "chatgpt_oauth",
            "clerk_user_id": "user_a",
        },
    )
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_awaited_once_with(
        "org_x",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id=None,
    )


@pytest.mark.asyncio
async def test_live_renewal_never_rewrites_provider_choice(mock_repo):
    """A live→live event must NOT persist metadata.provider_choice.

    Subscription metadata is a snapshot of the provider chosen at checkout
    time and is never updated by change_provider. Renewal invoices, seat
    syncs, and payment-method changes all fire .updated with that stale
    snapshot against an already-active row — persisting it clobbers a
    post-onboarding provider switch (and, via set_provider_choice's REMOVE,
    strips oauth_connector_user_id). The state sync itself still runs.
    """
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_not_called()
    mock_repo.set_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_activation_still_persists_provider_backup(mock_repo):
    """The .updated(active) that follows a skipped .created(incomplete) must
    still perform the provider backup write.

    For a trial_local owner, a checkout-born sub can surface first as
    .created(incomplete), which the stale-status guard skips entirely on the
    promise that the follow-up active event performs the deferred writes.
    That follow-up is an .updated — so the backup must key on the transition
    into a live status, not on the event type.
    """
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "subscription_status": "trial_local",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "chatgpt_oauth"},
    )
    event["type"] = "customer.subscription.updated"
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_awaited_once_with(
        "org_x",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        connector_user_id=None,
    )


@pytest.mark.asyncio
async def test_past_due_recovery_does_not_rewrite_provider_choice(mock_repo):
    """past_due → active payment recovery is not a fresh checkout — the row's
    provider (possibly switched while past_due) must survive it."""
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "subscription_status": "past_due",
        "provider_choice": "byo_key",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_not_called()


@pytest.mark.asyncio
async def test_unpaid_recovery_does_not_rewrite_provider_choice(mock_repo):
    """unpaid → active is an existing subscription reactivated by paying the
    invoice, not a fresh checkout — the backup must not run. Prior states are
    an allowlist so unknown/future Stripe statuses also default to skip."""
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "subscription_status": "unpaid",
        "provider_choice": "chatgpt_oauth",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_paid_row_does_not_rewrite_provider_choice(mock_repo):
    """A legacy paid row (subscription_status None but stripe_subscription_id
    set — is_subscription_active treats it as live) is NOT pre-live: it can
    have switched providers via /billing/provider, so an .updated(active)
    carrying checkout-time metadata must not re-apply it. Only a row with no
    subscription id at all counts as a fresh signup."""
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "stripe_subscription_id": "sub_legacy",
        "provider_choice": "byo_key",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_not_called()


@pytest.mark.asyncio
async def test_unpersisted_metadata_never_reaches_analytics(mock_repo):
    """When the provider backup write is skipped, the PostHog bridge must
    report the row's provider — not the stale checkout-time metadata.

    Otherwise a past_due → active recovery after a provider switch keeps the
    billing row intact but still stamps the person profile / lifecycle event
    with the checkout-time provider, corrupting provider segmentation for
    exactly the case the row guard protects.
    """
    mock_repo.get_by_owner_id.return_value = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "subscription_status": "past_due",
        "provider_choice": "byo_key",
    }
    event = _subscription_event(
        default_payment_method=None,
        status="active",
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    with patch.object(handlers, "capture_product_event") as capture:
        await handlers.handle_subscription_state(event, MagicMock())

    assert capture.call_count == 1
    kwargs = capture.call_args.kwargs
    assert kwargs["properties"]["provider_choice"] == "byo_key"
    assert kwargs["set_person"]["provider_choice"] == "byo_key"


@pytest.mark.asyncio
async def test_webhook_backup_write_refuses_the_retired_bedrock_choice(mock_repo):
    """Subscription metadata naming the retired bedrock_claude provider is not persisted."""
    event = _subscription_event(
        default_payment_method=None,
        metadata={
            "owner_id": "org_x",
            "provider_choice": "bedrock_claude",
            "clerk_user_id": "user_a",
        },
    )
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_not_called()


# ---------------------------------------------------------------------------
# Runtime-affecting billing changes dispatch ONE durable reconcile
# ---------------------------------------------------------------------------
#
# A trial_local -> paid conversion leaves the workspace on the trial provider in every profile.
# The handler commits the paid status (and provider/plan) first, then dispatches a durable
# reconcile execution, which re-derives the AIPs and rewrites every profile from that billing.
# There is no conversion marker, guard window or poll self-heal any more: the SFN execution is the
# durable owner, and a failed reconcile pages provision.reconcile.failed.


async def _run_state(
    mock_repo, prior_status, new_status, *, plan="standard", stored_sub=None, front_desk=False, **event_overrides
):
    """Drive handle_subscription_state across a status transition. Assert on the mocks."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": prior_status,
            "plan": plan,
            **({"stripe_subscription_id": stored_sub} if stored_sub else {}),
            **({"front_desk": True} if front_desk else {}),
        }
    )
    event = _subscription_event(default_payment_method=None, status=new_status, **event_overrides)
    await handlers.handle_subscription_state(event, MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["trialing", "active"])
async def test_trial_conversion_dispatches_one_durable_reconcile(mock_repo, reconcile_dispatch, new_status):
    await _run_state(mock_repo, "trial_local", new_status)

    reconcile_dispatch.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_conversion_reconcile_is_dispatched_after_billing_commits(mock_repo, reconcile_dispatch):
    """Invariant 1b: the reconcile renders profiles from billing, so the paid status, provider
    and plan must all be committed BEFORE the execution starts -- or it renders the trial shape."""
    order: list[str] = []
    mock_repo.set_subscription = AsyncMock(side_effect=lambda **_k: order.append("status"))
    mock_repo.set_provider_choice = AsyncMock(side_effect=lambda *_a, **_k: order.append("provider"))
    mock_repo.set_plan = AsyncMock(side_effect=lambda *_a, **_k: order.append("plan"))
    reconcile_dispatch.side_effect = lambda *_a: order.append("reconcile") or True

    # chatgpt_oauth: only chatgpt_oauth / byo_key metadata persist a provider since #3048.
    await _run_state(
        mock_repo, "trial_local", "active", metadata={"owner_id": "org_x", "provider_choice": "chatgpt_oauth"}
    )

    assert order == ["status", "provider", "plan", "reconcile"]


@pytest.mark.asyncio
async def test_conversion_relimits_the_openrouter_key_before_the_reconcile(mock_repo, reconcile_dispatch, monkeypatch):
    """Invariant 1a: a trial owner stopped at the $3/day OpenRouter valve (the key's daily limit
    plus the gate-stamped ``openrouter_capped_day``) is unblocked on payment. ``relimit_owner_key``
    moves the key onto the paid limit and clears the capped day, inline in the webhook; the
    reconcile repeats it (``ensure_owner_key_for_settings``) for a managed provider."""
    monkeypatch.setattr("core.config.settings.OPENROUTER_WORKSPACE_ID", "ws_x")
    order: list[str] = []
    relimit = AsyncMock(side_effect=lambda *_a, **_k: order.append("relimit"))
    monkeypatch.setattr("core.services.openrouter_keys.relimit_owner_key", relimit)
    reconcile_dispatch.side_effect = lambda *_a: order.append("reconcile") or True

    await _run_state(mock_repo, "trial_local", "active")

    relimit.assert_awaited_once_with("org_x")
    assert order == ["relimit", "reconcile"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["incomplete", "canceled", None])
async def test_every_entry_into_paid_dispatches_a_reconcile(mock_repo, reconcile_dispatch, prior_status):
    """A checkout-born sub whose .created surfaced as ``incomplete`` and a re-subscribe after cancel
    both enter paid on a later .updated(active): the reconcile is what ensures their Metronome
    customer and re-arms the nightly CEO."""
    await _run_state(mock_repo, prior_status, "active")

    reconcile_dispatch.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
@pytest.mark.parametrize(("prior_status", "new_status"), [("active", "active"), ("trialing", "active")])
async def test_every_live_event_reconciles(mock_repo, reconcile_dispatch, prior_status, new_status):
    """A redelivered conversion finds old_status already "active" (entering_live False, no plan
    diff), so every live event reconciles -- one idempotent reconcile per renewal event."""
    await _run_state(mock_repo, prior_status, new_status)

    reconcile_dispatch.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_a_non_live_event_without_a_plan_change_does_not_reconcile(mock_repo, reconcile_dispatch):
    await _run_state(mock_repo, "active", "past_due")

    reconcile_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dispatch_that_cannot_start_raises_so_stripe_redelivers(mock_repo, reconcile_dispatch, facts_mock):
    """The router rolls back the dedup row and 500s on a raise. Without it a paying owner would
    stay on the trial shape: the redelivery is the only thing that can dispatch again. The raise
    comes LAST, after the facts refresh, so nothing a redelivery would skip is left undone."""
    reconcile_dispatch.return_value = False

    with pytest.raises(RuntimeError, match="reconcile dispatch failed"):
        await _run_state(mock_repo, "trial_local", "active")

    mock_repo.set_subscription.assert_awaited_once()
    facts_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_plan_change_on_a_live_subscription_reconciles_the_runtime(mock_repo, reconcile_dispatch):
    """The plan decides the CEO's premium tier (and, for Base, the provider)."""
    await _run_state(mock_repo, "active", "active", plan="base")

    reconcile_dispatch.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["trial_local", "active", "trialing"])
@pytest.mark.parametrize("pending_status", ["incomplete", "incomplete_expired"])
async def test_incomplete_never_clobbers_access_granting_status(
    mock_repo, reconcile_dispatch, prior_status, pending_status
):
    """A Checkout-born subscription surfaces as `incomplete` before its first
    invoice settles (and `incomplete_expired` when checkout is abandoned).
    Neither may overwrite an access-granting status: over trial_local the
    write would make the follow-on active event skip the trial→paid
    conversion and strip a still-valid free-tier owner's access;
    over active/trialing it is a stale out-of-order event (Stripe webhook
    delivery is unordered) that would block a customer who already paid. The
    handler must skip ALL writes."""
    await _run_state(mock_repo, prior_status, pending_status)

    mock_repo.set_subscription.assert_not_called()
    mock_repo.set_provider_choice.assert_not_called()
    reconcile_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_persists_normally_for_non_trial_owners(mock_repo, reconcile_dispatch):
    """The trial_local guard is narrow: an incomplete event for an owner who
    was never on the free tier (e.g. a canceled account re-subscribing)
    persists exactly as before."""
    await _run_state(mock_repo, "canceled", "incomplete")

    mock_repo.set_subscription.assert_called_once()
    assert mock_repo.set_subscription.call_args.kwargs["status"] == "incomplete"
    reconcile_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_local_to_canceled_does_not_reconcile(mock_repo, reconcile_dispatch):
    """Only a transition into a paid-usable status (trialing/active) is a conversion."""
    await _run_state(mock_repo, "trial_local", "canceled")

    reconcile_dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# conversion telemetry: status changes vs. the first invoice paid at list (T23)
# ---------------------------------------------------------------------------


async def _run_state_capturing_telemetry(mock_repo, prior_status, new_status):
    with patch.object(handlers, "capture_product_event") as mock_capture:
        await _run_state(mock_repo, prior_status, new_status)
    return mock_capture


@pytest.mark.asyncio
async def test_card_on_file_trial_is_not_a_conversion(mock_repo):
    """trial_local -> trialing is a card on file with nothing paid (T05)."""
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "trial_local", "trialing")

    events = [c.kwargs.get("event") for c in mock_capture.call_args_list]
    assert "trial_converted" not in events
    assert "trial_started" in events


@pytest.mark.asyncio
async def test_trial_local_straight_to_paid_is_trial_checkout_completed(mock_repo):
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "trial_local", "active")
    calls = [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_checkout_completed"]
    assert len(calls) == 1
    assert calls[0].kwargs["properties"] == {
        "workspace_id": "org_x",
        "from_status": "trial_local",
        "to_status": "active",
        "provider_choice": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("prior", "new"), [("trial_local", "active"), ("trialing", "active"), ("active", "active")])
async def test_no_status_change_is_trial_converted(mock_repo, prior, new):
    """T23: trial_converted comes only from the first invoice paid at list, never a status."""
    mock_capture = await _run_state_capturing_telemetry(mock_repo, prior, new)
    assert [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_converted"] == []


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
async def test_subscription_state_refreshes_owner_facts(mock_repo, facts_mock):
    """A status flip (e.g. -> past_due) must rewrite the owner-facts file with
    the just-persisted status as an override — the row re-read is eventually
    consistent, and agents are told to trust the facts file."""
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(_subscription_event(status="past_due"), MagicMock())

    facts_mock.assert_awaited_once()
    args, kwargs = facts_mock.await_args
    assert args[0] == "org_x"
    assert kwargs["account_overrides"]["subscription_status"] == "past_due"


@pytest.mark.asyncio
async def test_subscription_state_facts_carry_backup_provider(mock_repo, facts_mock):
    """When the webhook backup path persists provider_choice from Stripe
    metadata, the facts refresh must carry the same provider override."""
    event = _subscription_event(status="active")
    event["data"]["object"]["metadata"]["provider_choice"] = "byo_key"
    event["data"]["object"]["metadata"]["byo_provider"] = "anthropic"
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())

    overrides = facts_mock.await_args.kwargs["account_overrides"]
    assert overrides["provider_choice"] == "byo_key"
    assert overrides["byo_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_facts_skip_provider_override_when_backup_write_fails(mock_repo, facts_mock):
    """If the swallowed set_provider_choice backup write fails, the facts
    refresh must NOT claim the metadata provider — the billing row never got
    it. Status still rides along."""
    mock_repo.set_provider_choice = AsyncMock(side_effect=RuntimeError("ddb down"))
    event = _subscription_event(status="active")
    event["data"]["object"]["metadata"]["provider_choice"] = "byo_key"
    event["data"]["object"]["metadata"]["byo_provider"] = "anthropic"
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())

    overrides = facts_mock.await_args.kwargs["account_overrides"]
    assert overrides == {"subscription_status": "active", "plan": "standard"}


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


# ---------------------------------------------------------------------------
# Dual-provider backstop: Stripe goes live while Apple already bills the owner
# ---------------------------------------------------------------------------


async def _run_with_apple(mock_repo, apple_status, new_status):
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": "active",
            "apple_subscription_status": apple_status,
        }
    )
    event = _subscription_event(default_payment_method=None, status=new_status)
    with patch.object(handlers, "put_metric") as metric:
        await handlers.handle_subscription_state(event, MagicMock())
    return [c.args[0] for c in metric.call_args_list]


@pytest.mark.asyncio
async def test_stripe_going_live_over_live_apple_emits_the_dual_provider_metric(mock_repo):
    names = await _run_with_apple(mock_repo, "active", "active")
    assert "billing.dual_provider_live" in names
    # Never blocks the Stripe write: the customer paid Stripe too.
    mock_repo.set_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_dual_provider_metric_when_apple_is_not_live(mock_repo):
    names = await _run_with_apple(mock_repo, "canceled", "active")
    assert "billing.dual_provider_live" not in names


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["active", "past_due", "canceled"])
async def test_a_stale_trialing_event_after_the_trial_is_ignored(mock_repo, prior):
    """A reordered trialing delivery must not put a post-trial workspace back on its trial (T05)."""
    await _run_state(mock_repo, prior, "trialing")
    mock_repo.set_subscription.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "canceled", "past_due"])
async def test_subscription_handler_calls_sync_front_desk_after_seats(mock_repo, monkeypatch, status):
    calls = MagicMock()
    seats = AsyncMock()
    front_desk = AsyncMock()
    calls.attach_mock(seats, "seats")
    calls.attach_mock(front_desk, "front_desk")
    monkeypatch.setattr("core.services.billing_service.sync_seat_quantity", seats)
    monkeypatch.setattr("core.services.text_lines.sync_front_desk", front_desk)
    monkeypatch.setattr("core.config.settings.STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    event = _subscription_event(status=status)
    event["data"]["object"]["items"]["data"].append({"id": "si_fd", "price": {"id": "price_fd"}})
    with patch.object(handlers, "stripe"):
        await handlers.handle_subscription_state(event, MagicMock())
    front_desk.assert_awaited_once_with(
        "org_x", subscription_id=event["data"]["object"]["id"], previous_subscription_id=None
    )
    if status == "active":
        assert [c[0] for c in calls.mock_calls] == ["seats", "front_desk"]


@pytest.mark.asyncio
async def test_a_claim_on_the_row_syncs_even_when_the_event_has_no_item(mock_repo, front_desk_sync):
    """The item was removed: the row's claim is what still owes a release."""
    await _run_state(mock_repo, "active", "active", front_desk=True)
    front_desk_sync.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "canceled"])
async def test_subscription_event_without_the_item_and_without_a_claim_never_calls_stripe_or_release(
    mock_repo, monkeypatch, status
):
    from core.services import text_lines

    monkeypatch.setattr(text_lines, "sync_front_desk", _REAL_SYNC)  # a call would reach Stripe or a release
    retrieve, release = MagicMock(), AsyncMock()
    monkeypatch.setattr(text_lines.stripe.Subscription, "retrieve", retrieve)
    monkeypatch.setattr(text_lines, "release_line", release)
    monkeypatch.setattr("core.services.billing_service.sync_seat_quantity", AsyncMock())
    await _run_state(mock_repo, "active", status)
    retrieve.assert_not_called()
    release.assert_not_awaited()


def _deleted(stored, *, front_desk=True):
    event = {
        "id": "evt_del",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_x", "customer": "cus_x", "metadata": {"owner_id": "org_x"}}},
    }
    row = {
        "owner_id": "org_x",
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        "stripe_subscription_id": stored,
        **({"front_desk": True} if front_desk else {}),
    }
    return event, row


async def _run_deleted(event, row, billing_service, release):
    """Drive handle_subscription_deleted through the real sync_front_desk. Stripe says the event's
    subscription (sub_x) is canceled and any other id the row stores is live."""
    from core.services import text_lines

    def retrieve_stub(sid, *_a, **_k):
        return {"status": "canceled" if sid == "sub_x" else "active", "items": {"data": []}}

    with (
        patch.object(handlers, "billing_repo") as repo,
        patch.object(text_lines.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(text_lines.stripe.Subscription, "retrieve", MagicMock(side_effect=retrieve_stub)) as retrieve,
        patch("core.services.text_lines.release_line", release),
        patch("core.services.hermes_provisioning.delete_workspace_runtime", AsyncMock()),
        patch(
            "core.services.entitlement_teardown.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "canceled"}),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(return_value=row)
        repo.set_plan = AsyncMock()
        await handlers.handle_subscription_deleted(event, billing_service)
    return retrieve


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "released"), [("sub_x", True), ("sub_old", False), (None, True)])
async def test_subscription_deleted_releases_the_line_only_for_the_current_subscription(
    facts_mock, monkeypatch, stored, released
):
    """The deleted event goes through sync_front_desk's one rule: a superseded subscription skips."""
    monkeypatch.setattr("core.services.text_lines.sync_front_desk", _REAL_SYNC)
    billing_service = MagicMock()
    order = []
    release = AsyncMock(side_effect=lambda *_a: order.append("release"))
    billing_service.cancel_subscription = AsyncMock(side_effect=lambda *_a: order.append("cancel"))
    event, row = _deleted(stored)
    await _run_deleted(event, row, billing_service, release)

    assert release.await_count == int(released)
    if released:
        release.assert_awaited_once_with("org_x")
        assert order == ["release", "cancel"]


@pytest.mark.asyncio
async def test_subscription_deleted_without_a_claim_never_calls_stripe(facts_mock, monkeypatch):
    monkeypatch.setattr("core.services.text_lines.sync_front_desk", _REAL_SYNC)
    billing_service = MagicMock()
    billing_service.cancel_subscription = AsyncMock()
    release = AsyncMock()
    event, row = _deleted("sub_x", front_desk=False)
    retrieve = await _run_deleted(event, row, billing_service, release)
    retrieve.assert_not_called()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_handler_hands_the_previously_stored_subscription_to_the_sync(mock_repo, front_desk_sync):
    """The superseded-subscription decision lives in sync_front_desk, on the fresh retrieve."""
    await _run_state(mock_repo, "active", "active", stored_sub="sub_old", front_desk=True, id="sub_new")
    front_desk_sync.assert_awaited_once_with("org_x", subscription_id="sub_new", previous_subscription_id="sub_old")
