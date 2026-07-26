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
        yield repo


@pytest.fixture(autouse=True)
def mock_container_repo():
    """The trial-conversion branch stamps the container row before it dispatches
    the reprovision. Stub the repo for EVERY test in this module so no handler
    path reaches real DynamoDB. Marker writes go through the SCOPED
    ``update_conversion_fields`` (never the get→put ``update_fields`` — that
    was the lease-erasing clobber, see the repo helper's docstring)."""
    with patch.object(handlers, "container_repo") as repo:
        repo.update_conversion_fields = AsyncMock(return_value=None)
        repo.update_fields = AsyncMock(return_value=None)
        repo.get_by_owner_id = AsyncMock(return_value=None)
        yield repo


@pytest.fixture(autouse=True)
def conversion_entry(monkeypatch):
    """Single-flight conversion-entry CAS (#1428). Default every test to
    WINNING the entry claim; individual tests flip the mock to ALREADY_SEEN
    to exercise the duplicate-event no-op. Returns (record_mock, delete_mock)."""
    record = AsyncMock(return_value=handlers.webhook_dedup.WebhookDedupResult.RECORDED)
    delete = AsyncMock()
    monkeypatch.setattr(handlers.webhook_dedup, "record_event_or_skip", record)
    monkeypatch.setattr(handlers.webhook_dedup, "delete_event", delete)
    return record, delete


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
async def test_webhook_backup_write_threads_connector(mock_repo):
    """The provider_choice backup write threads the subscribing human's Clerk
    id (from subscription metadata) as connector_user_id for chatgpt_oauth.

    create_flat_fee_checkout stamps clerk_user_id into subscription metadata;
    for chatgpt_oauth that human is the workspace owner / initial connector.
    Non-oauth providers pass None so the backup never strips a synchronously
    written connector.
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
        connector_user_id="user_a",
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
        metadata={"owner_id": "org_x", "provider_choice": "bedrock_claude"},
    )
    event["type"] = "customer.subscription.updated"
    # trial_local → active also dispatches the conversion reprovision; capture
    # and close the coroutine so this test stays scoped to the backup write.
    with patch.object(
        handlers.asyncio,
        "create_task",
        side_effect=lambda coro: coro.close() or MagicMock(),
    ):
        await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_awaited_once_with(
        "org_x",
        provider_choice="bedrock_claude",
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
async def test_webhook_backup_write_bedrock_passes_no_connector(mock_repo):
    """A bedrock subscription event passes connector_user_id=None — bedrock is
    not connector-backed, and None leaves any existing connector untouched."""
    event = _subscription_event(
        default_payment_method=None,
        metadata={
            "owner_id": "org_x",
            "provider_choice": "bedrock_claude",
            "clerk_user_id": "user_a",
        },
    )
    await handlers.handle_subscription_state(event, MagicMock())

    mock_repo.set_provider_choice.assert_awaited_once_with(
        "org_x",
        provider_choice="bedrock_claude",
        byo_provider=None,
        connector_user_id=None,
    )


class TestTeamSeatWebhook:
    """handle_subscription_state records item id + seat count for team subs."""

    @staticmethod
    def _team_event(**item_overrides):
        item = {"id": "si_1", "quantity": 4, "price": {"id": "price_seat_test"}}
        item.update(item_overrides)
        return {
            "id": "evt_team",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_team",
                    "customer": "cus_team",
                    "status": "active",
                    "metadata": {"owner_id": "org_team", "purpose": "team_seat"},
                    "items": {"data": [item]},
                }
            },
        }

    @pytest.mark.asyncio
    async def test_records_team_seat_state(self, monkeypatch):
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_team", "stripe_customer_id": "cus_team"})
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(self._team_event(), MagicMock())

        repo.set_team_subscription.assert_awaited_once_with(
            owner_id="org_team",
            subscription_id="sub_team",
            subscription_item_id="si_1",
            status="active",
            seat_count=4,
        )

    @pytest.mark.asyncio
    async def test_flat_sub_does_not_call_set_team_subscription(self, monkeypatch):
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        flat_event = {
            "id": "evt_flat",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_flat",
                    "customer": "cus_flat",
                    "status": "trialing",
                    "metadata": {"owner_id": "user_flat"},
                    "items": {"data": [{"id": "si_flat", "quantity": 1, "price": {"id": "price_flat_test"}}]},
                }
            },
        }
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_flat", "stripe_customer_id": "cus_flat"})
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(flat_event, MagicMock())

        repo.set_team_subscription.assert_not_awaited()

    _BASE_ITEM = {"id": "si_base", "quantity": 1, "price": {"id": "price_base_test"}}
    _SEAT_ITEM = {"id": "si_seat", "quantity": 4, "price": {"id": "price_seat_test"}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "items_order",
        [
            pytest.param([_BASE_ITEM, _SEAT_ITEM], id="base_first"),
            pytest.param([_SEAT_ITEM, _BASE_ITEM], id="seat_first"),
        ],
    )
    async def test_seat_persistence_selects_seat_item_not_position_zero(self, monkeypatch, items_order):
        """The SEAT item's id and quantity are stored regardless of which item
        Stripe puts at items[0] -- the actual invariant is that ORDER IS
        IRRELEVANT, selection is by price id alone.

        Regression guard: storing the base item's id makes sync_team_seat_quantity
        drive the base quantity, billing a 4-seat org $50x4 + $20 = $220/mo
        instead of $50 + $20x4 = $130/mo. Stripe does not guarantee line-item
        order, so selection must be by price id, never by position.
        """
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        event = {
            "id": "evt_team_ordered",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_team",
                    "customer": "cus_team",
                    "status": "active",
                    "metadata": {"owner_id": "org_team", "purpose": "team_seat"},
                    "items": {"data": items_order},
                }
            },
        }
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_team", "stripe_customer_id": "cus_team"})
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(event, MagicMock())

        repo.set_team_subscription.assert_awaited_once_with(
            owner_id="org_team",
            subscription_id="sub_team",
            subscription_item_id="si_seat",
            status="active",
            seat_count=4,
        )

    @pytest.mark.asyncio
    async def test_detects_via_seat_item_without_metadata_purpose(self, monkeypatch):
        """A Teams sub with NO metadata.purpose=='team_seat' at all (e.g. one
        created directly in the Stripe Dashboard for a comped/enterprise org,
        or one whose metadata was cleared) must still be detected as a team
        seat sub, by scanning ALL items for the seat price -- not just item[0].

        Regression guard for the UNDER-billing failure mode: with the base at
        items[0] and no metadata purpose, the old fallback
        (`first_price_id == seat_price`) evaluated False, so is_team_seat was
        False, set_team_subscription was never called, subscription_item_id
        was never persisted, and sync_team_seat_quantity silently no-op'd
        forever -- the org would never be billed for seats added after
        signup.
        """
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        event = {
            "id": "evt_team_no_metadata",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_team_dash",
                    "customer": "cus_team_dash",
                    "status": "active",
                    "metadata": {"owner_id": "org_dash"},  # no "purpose" key at all
                    "items": {"data": [self._BASE_ITEM, self._SEAT_ITEM]},
                }
            },
        }
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(
                return_value={"owner_id": "org_dash", "stripe_customer_id": "cus_team_dash"}
            )
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(event, MagicMock())

        repo.set_team_subscription.assert_awaited_once_with(
            owner_id="org_dash",
            subscription_id="sub_team_dash",
            subscription_item_id="si_seat",
            status="active",
            seat_count=4,
        )

    @pytest.mark.asyncio
    async def test_warns_when_metadata_says_team_seat_but_no_item_matches_price(self, monkeypatch, caplog):
        """metadata.purpose=='team_seat' but no item carries
        STRIPE_TEAM_SEAT_PRICE_ID (env var unset, or the Stripe seat price was
        rotated to a new id): must not silently freeze seat state -- it should
        surface a warning so a price rotation is visible in logs instead of
        manifesting only as mysteriously-stuck seat counts."""
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        event = {
            "id": "evt_team_rotated",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_team_rotated",
                    "customer": "cus_team_rotated",
                    "status": "active",
                    "metadata": {"owner_id": "org_rotated", "purpose": "team_seat"},
                    "items": {"data": [{"id": "si_old", "quantity": 4, "price": {"id": "price_seat_STALE"}}]},
                }
            },
        }
        with (
            patch.object(handlers, "billing_repo") as repo,
            patch.object(handlers, "stripe") as mock_stripe,
            caplog.at_level("WARNING", logger=handlers.logger.name),
        ):
            repo.get_by_owner_id = AsyncMock(
                return_value={"owner_id": "org_rotated", "stripe_customer_id": "cus_team_rotated"}
            )
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(event, MagicMock())

        repo.set_team_subscription.assert_not_awaited()
        assert any("no item matches STRIPE_TEAM_SEAT_PRICE_ID" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# trial_local -> paid conversion forces a cold reprovision off the trial box
# ---------------------------------------------------------------------------
#
# The no-card local trial runs a locked-down trial container. When the owner
# subscribes via /billing/trial-checkout, Stripe emits
# customer.subscription.created (status trialing/active) and this handler flips
# the persisted status off "trial_local". A RUNNING container's
# provision_user_container only force-redeploys the stale task def + config, so
# the handler must dispatch ecs_manager.reprovision_for_user, which scales to
# zero + drains first so the follow-on background_provision takes the
# scale-from-zero branch that re-registers the task def AND rewrites
# openclaw.json — by which point the stored status is already the paid one, so
# resolve_provider_choice reads the owner's real paid provider.


async def _run_state_capturing_reprovision(mock_repo, prior_status, new_status):
    """Drive handle_subscription_state with the ecs-manager singleton +
    asyncio.create_task patched, then RUN whatever coroutine got dispatched.

    The handler no longer create_task()s ``reprovision_for_user`` directly: it
    dispatches ``run_conversion_reprovision``, the wrapper that owns the
    converting marker's lifecycle. Awaiting the captured coroutine here keeps
    these tests asserting the same end behaviour (a reprovision for the owner,
    dispatched exactly once).

    Returns (reprovision_mock, created_list)."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": prior_status,
        }
    )
    event = _subscription_event(default_payment_method=None, status=new_status)
    mgr = MagicMock()
    mgr.reprovision_for_user = AsyncMock(return_value={"status": "reprovisioning"})
    created: list = []
    with (
        patch("core.containers.get_ecs_manager", return_value=mgr),
        patch.object(
            handlers.asyncio,
            "create_task",
            side_effect=lambda coro: created.append(coro) or MagicMock(),
        ),
    ):
        await handlers.handle_subscription_state(event, MagicMock())
        for coro in created:
            await coro
    return mgr.reprovision_for_user, created


async def _run_state_capturing_telemetry(mock_repo, prior_status, new_status):
    """Like ``_run_state_capturing_reprovision`` but returns the
    ``capture_product_event`` mock instead, for asserting on the
    trial_converted telemetry call specifically."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": prior_status,
        }
    )
    event = _subscription_event(default_payment_method=None, status=new_status)
    mgr = MagicMock()
    mgr.reprovision_for_user = AsyncMock(return_value={"status": "reprovisioning"})
    with (
        patch("core.containers.get_ecs_manager", return_value=mgr),
        patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: coro.close() or MagicMock()),
        patch.object(handlers, "capture_product_event") as mock_capture,
    ):
        await handlers.handle_subscription_state(event, MagicMock())
    return mock_capture


@pytest.mark.asyncio
async def test_trial_local_to_trialing_dispatches_reprovision(mock_repo):
    """Prior row trial_local + event trialing → reprovision_for_user
    dispatched exactly once for the owner."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "trial_local", "trialing")

    reprovision.assert_called_once_with("org_x")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_trial_local_to_active_dispatches_reprovision(mock_repo):
    """Prior row trial_local + event active → reprovision_for_user dispatched
    exactly once (the immediate-charge conversion path)."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "trial_local", "active")

    reprovision.assert_called_once_with("org_x")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_active_renewal_does_not_dispatch_reprovision(mock_repo):
    """Prior row active + event active (an ordinary renewal webhook) → NO
    reprovision. Reprovision churn on every renewal would bounce chat
    sessions for no config change."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "active", "active")

    reprovision.assert_not_called()
    assert created == []


@pytest.mark.asyncio
async def test_trialing_to_active_does_not_dispatch_reprovision(mock_repo):
    """Prior row trialing + event active (a real Stripe trial converting) → NO
    reprovision. Only the LOCAL meta trial (trial_local) runs the meta box; a
    Stripe-native trial was already a paid-provider container."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "trialing", "active")

    reprovision.assert_not_called()
    assert created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["trial_local", "active", "trialing"])
@pytest.mark.parametrize("pending_status", ["incomplete", "incomplete_expired"])
async def test_incomplete_never_clobbers_access_granting_status(mock_repo, prior_status, pending_status):
    """A Checkout-born subscription surfaces as `incomplete` before its first
    invoice settles (and `incomplete_expired` when checkout is abandoned).
    Neither may overwrite an access-granting status: over trial_local the
    write would make the follow-on active event skip the trial→paid
    conversion reprovision and strip a still-valid free-tier owner's access;
    over active/trialing it is a stale out-of-order event (Stripe webhook
    delivery is unordered) that would block a customer who already paid. The
    handler must skip ALL writes."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, prior_status, pending_status)

    mock_repo.set_subscription.assert_not_called()
    mock_repo.set_provider_choice.assert_not_called()
    reprovision.assert_not_called()
    assert created == []


@pytest.mark.asyncio
async def test_incomplete_persists_normally_for_non_trial_owners(mock_repo):
    """The trial_local guard is narrow: an incomplete event for an owner who
    was never on the free tier (e.g. a canceled account re-subscribing)
    persists exactly as before."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "canceled", "incomplete")

    mock_repo.set_subscription.assert_called_once()
    assert mock_repo.set_subscription.call_args.kwargs["status"] == "incomplete"
    reprovision.assert_not_called()


@pytest.mark.asyncio
async def test_trial_local_to_canceled_does_not_dispatch_reprovision(mock_repo):
    """Prior row trial_local + event canceled → NO reprovision. Only a
    transition into a paid-usable status (trialing/active) is a conversion."""
    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "trial_local", "canceled")

    reprovision.assert_not_called()
    assert created == []


# ---------------------------------------------------------------------------
# trial_converted telemetry (Task 16) — fires alongside the reprovision
# dispatch above, for the SAME trial_local -> paid transition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trial_local_to_trialing_fires_trial_converted_telemetry(mock_repo):
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "trial_local", "trialing")

    trial_converted_calls = [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_converted"]
    assert len(trial_converted_calls) == 1
    call = trial_converted_calls[0]
    assert call.kwargs["distinct_id"] == "org_x"
    assert call.kwargs["properties"] == {
        "from_status": "trial_local",
        "to_status": "trialing",
        "provider_choice": None,
    }


@pytest.mark.asyncio
async def test_trial_local_to_active_fires_trial_converted_telemetry(mock_repo):
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "trial_local", "active")

    trial_converted_calls = [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_converted"]
    assert len(trial_converted_calls) == 1
    assert trial_converted_calls[0].kwargs["distinct_id"] == "org_x"


@pytest.mark.asyncio
async def test_active_renewal_does_not_fire_trial_converted_telemetry(mock_repo):
    """An ordinary active->active renewal (not a trial_local conversion)
    must never fire trial_converted."""
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "active", "active")

    trial_converted_calls = [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_converted"]
    assert trial_converted_calls == []


@pytest.mark.asyncio
async def test_trialing_to_active_fires_only_the_generic_bridge_trial_converted(mock_repo):
    """A REAL Stripe trial converting (trialing -> active) is a different
    moment from the local no-card trial converting. The generic
    subscription-state bridge (``_bridge_subscription_state``, pre-existing)
    already fires its OWN "trial_converted" event for this transition —
    this test's job is to prove my new trial_local-gated branch does NOT
    ALSO fire a second one (old_status='trialing' != 'trial_local' excludes
    it): exactly one "trial_converted" capture, not two."""
    mock_capture = await _run_state_capturing_telemetry(mock_repo, "trialing", "active")

    trial_converted_calls = [c for c in mock_capture.call_args_list if c.kwargs.get("event") == "trial_converted"]
    assert len(trial_converted_calls) == 1


@pytest.mark.asyncio
async def test_trial_converted_telemetry_failure_does_not_break_webhook(mock_repo):
    """Best-effort posture: if the trial_converted capture raises (its FIRST
    call, inside the trial_local conversion branch), the surrounding
    try/except (shared with the reprovision dispatch) must swallow it — the
    webhook handler itself must not raise. The unrelated second capture call
    (the generic subscription-state bridge, at the end of the function)
    succeeds normally so this test isolates the one call site under test."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": "trial_local",
        }
    )
    event = _subscription_event(default_payment_method=None, status="active")
    mgr = MagicMock()
    mgr.reprovision_for_user = AsyncMock(return_value={"status": "reprovisioning"})
    with (
        patch("core.containers.get_ecs_manager", return_value=mgr),
        patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: coro.close() or MagicMock()),
        patch.object(
            handlers,
            "capture_product_event",
            side_effect=[RuntimeError("posthog boom"), None],
        ),
    ):
        # Must not raise.
        result = await handlers.handle_subscription_state(event, MagicMock())
    assert result is None


# ---------------------------------------------------------------------------
# Task 12 (a): the conversion must not race the /container/status auto-retry
# ---------------------------------------------------------------------------
#
# reprovision_for_user scales the trial container to zero and then BLOCKS in a
# ~90s drain wait — deliberately, because two live tasks would both hold the
# per-owner EFS WAL SQLite open (our documented corruption mode). Meanwhile the
# just-paid owner polls /container/status every ~3s, sees status="stopped", and
# — now that they're paid, so no gate blocks them — would dispatch their OWN
# background_provision with no drain wait. The webhook therefore stamps the
# container row BEFORE the stop so the status path knows a conversion owns it.


def _stamped_fields(update_conversion_fields_mock) -> dict:
    """Merge every marker-field dict the handler wrote through
    container_repo.update_conversion_fields (the scoped, lease-safe writer)."""
    merged: dict = {}
    for call in update_conversion_fields_mock.await_args_list:
        merged.update(call.args[1] if len(call.args) > 1 else call.kwargs["fields"])
    return merged


@pytest.mark.asyncio
async def test_conversion_stamps_row_before_dispatching_reprovision(mock_repo, mock_container_repo):
    """The trial_local -> paid branch stamps conversion_pending +
    substatus=converting + conversion_started_at on the container row, AWAITED
    inside the webhook (so the marker is durable even if the fire-and-forget
    task never runs), and only then dispatches the reprovision wrapper."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "stripe_customer_id": "cus_x",
            "subscription_status": "trial_local",
        }
    )
    order: list[str] = []
    mock_container_repo.update_conversion_fields = AsyncMock(side_effect=lambda *_a, **_k: order.append("stamp"))

    captured: list[str] = []

    def capture_task(coro):
        order.append("dispatch")
        captured.append(coro.__qualname__)
        coro.close()
        return MagicMock()

    event = _subscription_event(default_payment_method=None, status="active")
    with (
        patch("core.containers.get_ecs_manager", return_value=MagicMock()),
        patch.object(handlers.asyncio, "create_task", side_effect=capture_task),
    ):
        await handlers.handle_subscription_state(event, MagicMock())

    assert order == ["stamp", "dispatch"], f"marker must be written before the dispatch, got {order}"
    assert captured == ["run_conversion_reprovision"]
    fields = _stamped_fields(mock_container_repo.update_conversion_fields)
    assert fields["conversion_pending"] is True
    assert fields["substatus"] == handlers.CONVERTING_SUBSTATUS
    assert fields["conversion_started_at"]


@pytest.mark.asyncio
async def test_non_conversion_event_does_not_stamp_the_container_row(mock_repo, mock_container_repo):
    """An ordinary active->active renewal never touches the container row."""
    await _run_state_capturing_reprovision(mock_repo, "active", "active")
    mock_container_repo.update_conversion_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_reprovision_success_clears_substatus_and_pending(mock_container_repo):
    """Success: the converting marker is cleared AND conversion_pending drops,
    so the self-heal below never re-fires a conversion that already landed."""
    mgr = MagicMock()
    mgr.reprovision_for_user = AsyncMock(return_value={"status": "reprovisioning"})
    with patch("core.containers.get_ecs_manager", return_value=mgr):
        await handlers.run_conversion_reprovision("org_x")

    mgr.reprovision_for_user.assert_awaited_once_with("org_x")
    fields = _stamped_fields(mock_container_repo.update_conversion_fields)
    assert fields["substatus"] is None
    assert fields["conversion_pending"] is False


@pytest.mark.asyncio
async def test_reprovision_failure_clears_substatus_but_keeps_pending(mock_container_repo):
    """Failure: the substatus is STILL cleared (never strand the row in a state
    nothing acts on), but conversion_pending survives — it is the flag the
    status-path self-heal keys off to re-fire the dead conversion. The task
    itself must not raise (it's a fire-and-forget create_task target)."""
    mgr = MagicMock()
    mgr.reprovision_for_user = AsyncMock(side_effect=RuntimeError("ecs boom"))
    with patch("core.containers.get_ecs_manager", return_value=mgr):
        await handlers.run_conversion_reprovision("org_x")  # must not raise

    fields = _stamped_fields(mock_container_repo.update_conversion_fields)
    assert fields["substatus"] is None
    assert "conversion_pending" not in fields, "a failed conversion must stay pending so the self-heal re-fires it"


# ---------------------------------------------------------------------------
# Task 12 (b): self-heal a conversion whose fire-and-forget task died
# ---------------------------------------------------------------------------


def _stale_ts() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=handlers.CONVERSION_GUARD_SECONDS + 60)).isoformat()


def _fresh_ts() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def test_conversion_guard_does_not_wedge_on_a_bare_converting_substatus():
    """A `converting` substatus with NO live timestamp is a DIED conversion, not
    an in-flight one — `_stamp_conversion_dispatch` always writes both. Treating
    the substatus alone as guard-active was unbounded: it froze the status-path
    auto-retry and every start path forever, stranding a PAYING customer on a
    trial container with only a manual admin lever. The guard must self-release."""
    assert handlers.conversion_guard_active({"substatus": handlers.CONVERTING_SUBSTATUS}) is False


def test_conversion_guard_active_within_window_even_without_substatus():
    """Load-bearing: ecs_manager.stop_user_service writes
    update_status(owner, "stopped") — substatus defaults to None — so it
    CLOBBERS the converting substatus the instant the drain begins. The
    timestamp, not the substatus, is what actually holds the auto-retry off
    during the ~90s drain."""
    assert handlers.conversion_guard_active({"substatus": None, "conversion_started_at": _fresh_ts()}) is True


def test_conversion_guard_inactive_on_a_plain_row():
    assert handlers.conversion_guard_active({"status": "stopped", "substatus": None}) is False
    assert handlers.conversion_guard_active({"conversion_started_at": _stale_ts()}) is False
    assert handlers.conversion_guard_active(None) is False


@pytest.mark.asyncio
async def test_self_heal_redispatches_stuck_conversion(mock_repo, mock_container_repo):
    """Paid billing + a container still carrying conversion_pending past the
    guard window == the conversion task died (backend deploy mid-drain, ECS
    error, a raise anywhere). Stripe will never re-fire the branch, so the
    status path re-dispatches it."""
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "active"})
    container = {"status": "running", "conversion_pending": True, "conversion_started_at": _stale_ts()}
    captured: list[str] = []

    with patch.object(
        handlers.asyncio,
        "create_task",
        side_effect=lambda coro: captured.append(coro.__qualname__) or coro.close() or MagicMock(),
    ):
        held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is True, "a re-dispatched conversion owns the row — the caller must not auto-retry"
    assert captured == ["run_conversion_reprovision"]
    fields = _stamped_fields(mock_container_repo.update_conversion_fields)
    assert fields["conversion_started_at"], "the re-dispatch must re-stamp the rate-limit anchor"
    assert container["substatus"] == handlers.CONVERTING_SUBSTATUS


@pytest.mark.asyncio
async def test_self_heal_is_rate_limited_inside_the_guard_window(mock_repo, mock_container_repo):
    """A fresh stamp means an in-flight (or just re-dispatched) conversion:
    hands off, no second dispatch. The status poll runs every ~3s — without
    this the self-heal would be a reprovision storm."""
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "active"})
    container = {"status": "stopped", "conversion_pending": True, "conversion_started_at": _fresh_ts()}
    captured: list = []

    with patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: captured.append(coro)):
        held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is True
    assert captured == []
    mock_container_repo.update_conversion_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_heal_drops_pending_when_billing_is_no_longer_paid(mock_repo, mock_container_repo):
    """Billing must SAY PAID for the self-heal to fire. A canceled owner's
    stale pending flag is dropped (not re-dispatched) so the row falls back to
    the ordinary auto-retry + gate path."""
    mock_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_x", "subscription_status": "canceled"})
    container = {"status": "stopped", "conversion_pending": True, "conversion_started_at": _stale_ts()}
    captured: list = []

    with patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: captured.append(coro)):
        held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is False
    assert captured == []
    assert _stamped_fields(mock_container_repo.update_conversion_fields) == {"conversion_pending": False}


@pytest.mark.asyncio
async def test_self_heal_noop_on_a_row_with_no_conversion_marker(mock_repo, mock_container_repo):
    """The hot path: an ordinary container row costs zero extra DDB reads and
    never holds off the auto-retry."""
    container = {"status": "stopped", "substatus": None}
    held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is False
    mock_repo.get_by_owner_id.assert_not_awaited()
    mock_container_repo.update_conversion_fields.assert_not_awaited()


def test_conversion_guard_self_releases_when_the_conversion_dies():
    """A conversion that dies after stamping must NOT wedge the row forever.

    The substatus alone used to return guard-active unconditionally, so a stale
    `converting` marker (dispatch died, backend redeployed mid-drain) froze the
    status-path auto-retry AND every start path — stranding a PAYING customer on
    a trial container with only a manual admin lever. The timestamp window is
    what makes the guard self-releasing.
    """
    from datetime import datetime, timedelta, timezone

    from core.services.stripe_webhook_handlers import (
        CONVERSION_GUARD_SECONDS,
        CONVERTING_SUBSTATUS,
        conversion_guard_active,
    )

    stale = (datetime.now(timezone.utc) - timedelta(seconds=CONVERSION_GUARD_SECONDS + 60)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    # A stale stamp releases the guard, even with the substatus still set.
    assert (
        conversion_guard_active(
            {"substatus": CONVERTING_SUBSTATUS, "conversion_started_at": stale},
        )
        is False
    )
    # A substatus with NO stamp at all also releases (it cannot wedge).
    assert conversion_guard_active({"substatus": CONVERTING_SUBSTATUS}) is False
    # A live conversion still holds the guard.
    assert (
        conversion_guard_active(
            {"substatus": CONVERTING_SUBSTATUS, "conversion_started_at": fresh},
        )
        is True
    )
    # ...and the stop wiping the substatus does not release it early.
    assert conversion_guard_active({"conversion_started_at": fresh}) is True


# ---------------------------------------------------------------------------
# #1428 layer 1: single-flight the conversion ENTRY across concurrent events
# ---------------------------------------------------------------------------
#
# Stripe delivers several events per subscription change (.updated,
# invoice-driven .updated, metadata-stamping .updated ...), unordered and
# near-simultaneous — possibly on two backend tasks. Each handler reads the
# billing row's old_status before any of them persists the new one, so more
# than one event can see trial_local -> active and enter the conversion
# branch. The entry CAS (webhook-dedup conditional put keyed on
# owner + subscription id) admits exactly one.


@pytest.mark.asyncio
async def test_conversion_entry_claim_uses_owner_and_subscription_key(mock_repo, conversion_entry):
    """The CAS key is (owner, subscription id) — per subscription TRANSITION,
    so a later re-subscribe (new sub id) gets a fresh claim."""
    record, _delete = conversion_entry
    await _run_state_capturing_reprovision(mock_repo, "trial_local", "active")

    record.assert_awaited_once_with("trial-conversion:org_x:sub_x", source="stripe-trial-conversion")


@pytest.mark.asyncio
async def test_second_conversion_event_noops_when_entry_already_claimed(
    mock_repo, mock_container_repo, conversion_entry
):
    """THE #1428 double-entry: a sibling event that finds the entry claim
    already recorded must no-op the WHOLE conversion block — no stamp, no
    reprovision dispatch, no duplicate trial_converted capture — while the
    ordinary subscription-state sync still runs."""
    record, _delete = conversion_entry
    record.return_value = handlers.webhook_dedup.WebhookDedupResult.ALREADY_SEEN

    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": "trial_local",
        }
    )
    event = _subscription_event(default_payment_method=None, status="active")
    event["type"] = "customer.subscription.updated"
    captured: list = []
    with (
        patch("core.containers.get_ecs_manager", return_value=MagicMock()),
        patch.object(
            handlers.asyncio,
            "create_task",
            side_effect=lambda coro: captured.append(coro.__qualname__) or coro.close() or MagicMock(),
        ),
        patch.object(handlers, "capture_product_event") as capture,
    ):
        await handlers.handle_subscription_state(event, MagicMock())

    assert captured == [], "duplicate event must not dispatch a second reprovision"
    mock_container_repo.update_conversion_fields.assert_not_awaited()
    assert [c for c in capture.call_args_list if c.kwargs.get("event") == "trial_converted"] == []
    # The state sync itself is untouched by the dedup.
    mock_repo.set_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversion_entry_cas_failure_fails_open(mock_repo, conversion_entry):
    """A DDB blip in the entry CAS must not strand a paying owner on the trial
    box: the conversion proceeds un-deduped (the provision lease is the next
    single-flight layer down)."""
    record, _delete = conversion_entry
    record.side_effect = RuntimeError("ddb down")

    reprovision, created = await _run_state_capturing_reprovision(mock_repo, "trial_local", "active")

    reprovision.assert_called_once_with("org_x")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_conversion_entry_rolled_back_when_stamp_fails(mock_repo, mock_container_repo, conversion_entry):
    """If the awaited stamp blows up before the dispatch, the entry claim is
    rolled back (delete_event) so a sibling event / Stripe redelivery can
    re-enter instead of finding the claim of a conversion that never started."""
    _record, delete = conversion_entry
    mock_container_repo.update_conversion_fields = AsyncMock(side_effect=RuntimeError("ddb down"))

    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "org_x",
            "owner_type": "org",
            "stripe_customer_id": "cus_x",
            "subscription_status": "trial_local",
        }
    )
    event = _subscription_event(default_payment_method=None, status="active")
    with (
        patch("core.containers.get_ecs_manager", return_value=MagicMock()),
        patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: coro.close() or MagicMock()),
    ):
        # Must not raise (best-effort posture).
        await handlers.handle_subscription_state(event, MagicMock())

    delete.assert_awaited_once_with("trial-conversion:org_x:sub_x")


# ---------------------------------------------------------------------------
# #1428 layer 3: the stranded illegal steady-state must not hide behind the
# conversion guard
# ---------------------------------------------------------------------------
#
# Terminal state observed live: status="stopped", ALL lease fields cleared,
# last_error null, conversion_pending dropped, guard window still fresh —
# nothing running and nothing scheduled. Honouring the guard there stands the
# poll's wake path down for the full window on a row nobody owns.


@pytest.mark.asyncio
async def test_stranded_stopped_row_releases_the_guard(mock_repo, mock_container_repo):
    """stopped + no claim + no last_error + guard fresh + pending dropped ==
    the #1428 strand. maybe_resume must return False so the caller's wake
    branch (single-flight underneath) heals it NOW, not after the window."""
    container = {"status": "stopped", "conversion_pending": False, "conversion_started_at": _fresh_ts()}

    held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is False
    # Costs nothing: no billing read, no row write.
    mock_repo.get_by_owner_id.assert_not_awaited()
    mock_container_repo.update_conversion_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_row_with_successor_claim_keeps_the_guard(mock_repo):
    """A claim on the row means an SFN execution owns it — the guard holds so
    the poll cannot race a provision on top of the successor."""
    container = {
        "status": "stopped",
        "conversion_pending": False,
        "conversion_started_at": _fresh_ts(),
        "provision_claim_id": "claim-successor",
    }
    assert await handlers.maybe_resume_stuck_conversion("org_x", container) is True


@pytest.mark.asyncio
async def test_stopped_row_with_last_error_keeps_the_guard(mock_repo):
    """A recorded error means some release already landed — the ordinary error
    auto-retry owns recovery once the guard lapses; no early release needed."""
    container = {
        "status": "stopped",
        "conversion_pending": False,
        "conversion_started_at": _fresh_ts(),
        "last_error": "Paperclip 502: upstream",
    }
    assert await handlers.maybe_resume_stuck_conversion("org_x", container) is True


@pytest.mark.asyncio
async def test_drain_window_is_not_exposed_by_the_stranded_release(mock_repo):
    """During the conversion's drain the row is stopped + no claim — but
    conversion_pending is still True, which routes through the pending branch
    (rate-limited stand-down), NOT the stranded release. The EFS-WAL
    single-writer guarantee stays intact."""
    container = {"status": "stopped", "conversion_pending": True, "conversion_started_at": _fresh_ts()}
    captured: list = []
    with patch.object(handlers.asyncio, "create_task", side_effect=lambda coro: captured.append(coro)):
        held = await handlers.maybe_resume_stuck_conversion("org_x", container)

    assert held is True
    assert captured == []


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
    assert overrides == {"subscription_status": "active"}


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
        patch("core.containers.get_ecs_manager") as ecs,
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning",
            AsyncMock(side_effect=RuntimeError("skip paperclip teardown")),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "org_x", "owner_type": "org", "stripe_customer_id": "cus_x"}
        )
        repo.set_plan = AsyncMock()
        ecs.return_value.delete_user_service = AsyncMock()
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
        patch("core.containers.get_ecs_manager") as ecs,
        patch(
            "core.services.paperclip_provisioning.build_paperclip_provisioning",
            AsyncMock(side_effect=RuntimeError("skip paperclip teardown")),
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
        ecs.return_value.delete_user_service = AsyncMock()
        await handlers.handle_subscription_deleted(event, billing_service)

    repo.set_plan.assert_awaited_once()
    assert repo.set_plan.await_args.args[0] == "org_u"
    assert repo.set_plan.await_args.kwargs["plan"] == "standard"


@pytest.mark.asyncio
async def test_facts_carry_team_seat_fields_when_persisted(mock_repo, facts_mock, monkeypatch):
    """A fresh Teams seat sub must ride subscription_item_id/seat_count into
    the facts overrides — the eventually-consistent row re-read can miss the
    just-committed fields and mislabel the plan as flat-fee."""
    monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
    mock_repo.set_team_subscription = AsyncMock()
    event = _subscription_event(status="active")
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"] = {"data": [{"id": "si_9", "quantity": 3, "price": {"id": "price_seat_test"}}]}
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())

    overrides = facts_mock.await_args.kwargs["account_overrides"]
    assert overrides["subscription_item_id"] == "si_9"
    assert overrides["seat_count"] == 3


@pytest.mark.asyncio
async def test_facts_skip_team_seat_fields_when_write_fails(mock_repo, facts_mock, monkeypatch):
    monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
    mock_repo.set_team_subscription = AsyncMock(side_effect=RuntimeError("ddb down"))
    event = _subscription_event(status="active")
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"] = {"data": [{"id": "si_9", "quantity": 3, "price": {"id": "price_seat_test"}}]}
    with patch.object(handlers, "stripe") as mock_stripe:
        mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}
        await handlers.handle_subscription_state(event, MagicMock())

    overrides = facts_mock.await_args.kwargs["account_overrides"]
    assert "subscription_item_id" not in overrides
    assert "seat_count" not in overrides
