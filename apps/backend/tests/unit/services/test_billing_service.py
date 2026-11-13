"""Tests for BillingService — flat-fee model."""

from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from core.services import billing_service
from core.services.billing_service import (
    BillingService,
)


@pytest.fixture(autouse=True)
def _checkout_slot_claim_granted():
    """Checkout paths serialize session creation behind a
    per-owner DDB claim. Grant it by default so every existing checkout test
    exercises the path it was written for; the claim's own behavior is
    tested explicitly in TestCheckoutSlotClaim."""
    with (
        patch.object(
            billing_service.billing_repo,
            "try_claim_checkout_slot",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            billing_service.billing_repo,
            "release_checkout_slot",
            new=AsyncMock(),
        ),
        patch.object(
            billing_service.billing_repo,
            "set_pending_checkout",
            new=AsyncMock(),
        ),
    ):
        yield


@pytest.fixture
def dynamodb_billing_table():
    """Moto-backed billing-accounts table (mirrors
    tests/unit/repositories/test_billing_repo.py's fixture) for the one
    cancel_subscription test that needs real DynamoDB read-then-put
    semantics rather than a mocked billing_repo."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
                {"AttributeName": "apple_original_transaction_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "apple-original-transaction-index",
                    "KeySchema": [{"AttributeName": "apple_original_transaction_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-billing-accounts")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


class TestBillingServicePortal:
    @pytest.mark.asyncio
    @patch("core.services.billing_service.stripe")
    async def test_create_portal_session(self, mock_stripe):
        mock_stripe.billing_portal.Session.create.return_value = MagicMock(url="https://billing.stripe.com/test")
        url = await BillingService().create_portal_session(
            billing_account={"owner_id": "user_portal", "stripe_customer_id": "cus_portal"}
        )
        assert url == "https://billing.stripe.com/test"


class TestBillingServiceCancelSubscription:
    """Cancel writes status='canceled' and clears the subscription id via set_subscription."""

    @pytest.mark.asyncio
    @patch("core.services.billing_service.billing_repo")
    async def test_cancel_subscription(self, mock_repo):
        mock_repo.set_subscription = AsyncMock()
        await BillingService().cancel_subscription({"owner_id": "user_sub", "stripe_subscription_id": "sub_123"})
        mock_repo.set_subscription.assert_called_once_with(
            owner_id="user_sub",
            subscription_id=None,
            status="canceled",
            trial_end=None,
        )


@pytest.mark.asyncio
async def test_build_subscription_checkout_passes_line_items_verbatim(monkeypatch):
    """The builder forwards its line_items list to Stripe untouched — this is
    what lets the team path add a base-price item alongside the seat item in
    a later task without the builder itself changing."""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return MagicMock(id="cs_test_1", url="https://checkout.stripe.com/x")

    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )

    items = [{"price": "price_base", "quantity": 1}, {"price": "price_seat", "quantity": 3}]
    await billing_service._build_subscription_checkout(
        owner_id="org_1",
        line_items=items,
        metadata={"owner_id": "org_1"},
        success_url="https://x/ok",
        cancel_url="https://x/no",
        idempotency_key="k1",
    )

    assert captured["line_items"] == items


@pytest.mark.asyncio
async def test_change_provider_trial_local_refuses_use_trial_checkout(monkeypatch):
    """A trial_local row converts via /billing/trial-checkout, never the
    post-onboarding provider-switch path. change_provider must reject it with
    a use_trial_checkout / 409 BEFORE any Stripe or reprovision work.

    (The router maps ChangeProviderError → HTTP 409 with
    detail.code == "use_trial_checkout".)"""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "user_x",
                "subscription_status": "trial_local",
            }
        ),
    )
    # Guards must fire before either of these does any work.
    set_choice = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", set_choice)
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="user_x",
            clerk_user_id="user_x",
            owner_type="personal",
            provider_choice="bedrock_claude",
        )

    assert exc.value.code == "use_trial_checkout"
    assert exc.value.status_code == 409
    # No provider write, no stop, no reprovision dispatch.
    set_choice.assert_not_awaited()
    fake_manager.stop_user_service.assert_not_awaited()
    fake_manager.drain_then_dispatch_provision.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openrouter_managed", "chatgpt_oauth"])
@pytest.mark.parametrize("legacy_status", ["trial_local", None])
async def test_paid_voice_can_select_provider_without_legacy_subscription(
    monkeypatch, dynamodb_billing_table, provider, legacy_status
):
    """A live voice-plan subscription pays for access even on the original trial row."""
    account = {
        "owner_id": "org_voice",
        "owner_type": "org",
        "subscription_status": legacy_status,
        "trial_end": 1,
        "metronome_voice_contract_id": "voice-contract",
        "voice_plan_status": "active",
        "voice_plan_stripe_status": "active",
    }
    dynamodb_billing_table.put_item(Item=account)
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr("core.billing.openrouter_gate_url.gate_url", lambda: "https://gate.example/v1")
    key = AsyncMock()
    monkeypatch.setattr("core.services.openrouter_keys.ensure_owner_key_for_settings", key)
    tokens = AsyncMock(return_value={"access_token": "fixture-token"})
    monkeypatch.setattr("core.services.oauth_service.get_decrypted_tokens", tokens)
    monkeypatch.setattr(billing_service.billing_repo, "publish_status_changed", MagicMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    reconcile = AsyncMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", reconcile)

    result = await billing_service.change_provider(
        owner_id="org_voice", clerk_user_id="user_voice", owner_type="org", provider_choice=provider
    )

    assert result["status"] == "ok"
    saved = dynamodb_billing_table.get_item(Key={"owner_id": "org_voice"})["Item"]
    assert saved.pop("provider_choice") == provider
    saved.pop("updated_at")
    if provider == "chatgpt_oauth":
        assert saved.pop("oauth_connector_user_id") == "user_voice"
        tokens.assert_awaited_once_with(user_id="user_voice", bound_to="org_voice")
        key.assert_not_awaited()
    else:
        key.assert_awaited_once_with("org_voice")
        tokens.assert_not_awaited()
    # Provider selection must not invent a Workspace subscription, credits or payment state.
    assert saved == account
    reconcile.assert_awaited_once_with("org_voice")


@pytest.mark.asyncio
async def test_managed_switch_ensures_owner_key_before_provider_commit(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    order = []
    monkeypatch.setattr(
        "core.services.openrouter_keys.ensure_owner_key_for_settings",
        AsyncMock(side_effect=lambda owner: order.append("key")),
    )
    monkeypatch.setattr(
        billing_service.billing_repo,
        "set_provider_choice",
        AsyncMock(side_effect=lambda *a, **kw: order.append("commit")),
    )
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", AsyncMock())

    await billing_service.change_provider(
        owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="openrouter_managed"
    )
    assert order == ["key", "commit"]


@pytest.mark.asyncio
async def test_managed_key_failure_keeps_provider_unchanged(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.services.openrouter_keys.ensure_owner_key_for_settings",
        AsyncMock(side_effect=RuntimeError("upstream unavailable")),
    )
    commit = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", commit)

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="openrouter_managed"
        )
    assert exc.value.code == "managed_key_unavailable"
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_switch_requires_a_selected_server_model_before_commit(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_MODEL_ID", "")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    commit = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", commit)
    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="openrouter_managed"
        )
    assert exc.value.code == "managed_route_unavailable"
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reselecting_the_committed_provider_reruns_the_reconcile(monkeypatch):
    """Re-selecting is how an owner retries a switch whose reconcile was lost: it commits
    nothing and schedules the idempotent reconcile again."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    reconcile = AsyncMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", reconcile)

    result = await billing_service.change_provider(
        owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="bedrock_claude"
    )
    assert result["status"] == "noop"
    billing_service.billing_repo.set_provider_choice.assert_not_awaited()
    reconcile.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_a_switch_commits_billing_then_schedules_the_reconcile(monkeypatch):
    """No switch marker, no dispatch: the committed row is the only state, and the reconcile
    reads it."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    commit = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", commit)
    monkeypatch.setattr("core.services.key_service.get_secret_arn", AsyncMock(return_value="arn:key"))
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    reconcile = AsyncMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", reconcile)
    dispatch = AsyncMock(side_effect=AssertionError("a switch no longer dispatches a provision"))
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)

    result = await billing_service.change_provider(
        owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="byo_key", byo_provider="openai"
    )
    assert result["status"] == "ok"
    commit.assert_awaited_once()
    reconcile.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_change_provider_refreshes_owner_facts(monkeypatch):
    """A committed provider switch rewrites the owner-facts file, so agents
    reading it via the isol8 skill see the NEW provider on both paths."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    facts_mock = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", facts_mock)
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: coro.close() or MagicMock(),
    )

    await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    facts_mock.assert_awaited_once_with(
        "user_x",
        account_overrides={"provider_choice": "bedrock_claude", "byo_provider": None},
    )


class TestLegacyFlatCheckoutIdempotencyKey:
    """The retained legacy builder uses one stable key per 5-minute bucket."""

    # Frozen so the 5-minute bucket cannot roll between two calls and fake a
    # difference (or hide a sameness).
    NOW = 1_800_000_000.0

    @classmethod
    async def _key_for(cls) -> str:
        created = MagicMock(return_value=MagicMock(id="cs_1"))
        with (
            patch("core.services.billing_service.time.time", return_value=cls.NOW),
            patch.object(
                billing_service.billing_repo,
                "get_by_owner_id",
                new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
            ),
            patch.object(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_flat"),
            patch("stripe.checkout.Session.create", new=created),
        ):
            await billing_service._build_subscription_checkout(
                owner_id="u_1",
                line_items=[{"price": "price_flat", "quantity": 1}],
                metadata={"owner_id": "u_1"},
                success_url="https://x/ok",
                cancel_url="https://x/no",
                idempotency_key=billing_service._checkout_idempotency_key("flat_checkout", "u_1", "bedrock_claude:_"),
            )
        return created.call_args.kwargs["idempotency_key"]

    @pytest.mark.asyncio
    async def test_flat_checkout_key_is_untouched(self):
        """The current flat-checkout key stays stable within its time bucket."""
        key = await self._key_for()
        with patch("core.services.billing_service.time.time", return_value=self.NOW):
            unchanged = billing_service._checkout_idempotency_key("flat_checkout", "u_1", "bedrock_claude:_")
        assert key == unchanged
