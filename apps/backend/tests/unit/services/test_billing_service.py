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
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
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
async def test_change_provider_into_bedrock_takes_cold_path(monkeypatch):
    """A switch INTO bedrock_claude must NOT take the hot path (which would
    call build_provider_block(bedrock, aip_arns=None) and raise), and must
    NOT raise — it stop-first reprovisions (cold path, F1 fix)."""
    # No prior provider_choice -> none of the OLD-provider side-effect cleanup
    # blocks (oauth, byo) fire, keeping the test free of oauth/key I/O while
    # still exercising the bedrock hot-vs-cold selector.
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
    # change_provider imports is_subscription_active LAZILY from
    # core.services.provision_gate, so patch it at the source module.
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    hot = AsyncMock()
    monkeypatch.setattr(billing_service, "_hot_swap_provider", hot)
    # change_provider's cold path lazily imports get_ecs_manager from
    # core.containers, awaits .stop_user_service, and dispatches
    # .drain_then_dispatch_provision(owner_id) as a background task (F1
    # fix) — patch at the source module, and capture the dispatched task
    # instead of scheduling it so nothing outlives the test loop.
    fake_manager = MagicMock()
    fake_manager.stop_user_service = AsyncMock()
    fake_manager.drain_then_dispatch_provision = MagicMock()
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    created: list = []
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: created.append(coro) or MagicMock(),
    )

    result = await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    hot.assert_not_awaited()  # hot path skipped for bedrock target
    assert result["restart"] is True
    # No container row: there is nothing to stop, and issuing the stop anyway
    # would open a TOCTOU (a poll creating the row between the fence read and
    # the stop would get an unfenced "stopped" stamp). So the stop is SKIPPED
    # and the cold path falls straight through to the drain-only dispatch,
    # whose create-from-scratch claim provisions the owner.
    fake_manager.stop_user_service.assert_not_awaited()
    fake_manager.drain_then_dispatch_provision.assert_called_once_with("user_x")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_hot_swap_pre_stages_under_owner_id(monkeypatch):
    """One ChatGPT account ↔ one container: the token READ is keyed on the
    switcher (the connector), but the EFS auth file is WRITTEN under the
    container OWNER (org_id for orgs). For an org the two ids differ — pin
    that the read uses clerk_user_id and the write uses owner_id."""
    get_tokens = AsyncMock(return_value={"access_token": "tok", "account_id": "acc"})
    monkeypatch.setattr("core.services.oauth_service.get_decrypted_tokens", get_tokens)
    pre_stage = AsyncMock()
    monkeypatch.setattr("core.containers.workspace.pre_stage_auth_profile_store", pre_stage)
    # Stub the config side so the helper reaches the patch step without real
    # config I/O. build_provider_block returns the (providers, default, plugins)
    # triple; replace_provider_block is the EFS write we don't care about here.
    monkeypatch.setattr(
        "core.containers.config.build_provider_block",
        lambda **kwargs: ({}, {"primary": "p", "fallbacks": ["p"]}, {}),
    )
    monkeypatch.setattr("core.containers.config.premium_model_ref", lambda *a, **k: None)
    monkeypatch.setattr("core.containers.config.cheap_model_ref", lambda *a, **k: "cheap")
    monkeypatch.setattr("core.services.config_patcher.replace_provider_block", AsyncMock())
    fake_manager = MagicMock()
    fake_manager._deployed_image_tag = AsyncMock(return_value=None)
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)

    await billing_service._hot_swap_provider(
        owner_id="org_1",
        clerk_user_id="user_a",
        new_provider="chatgpt_oauth",
    )

    # Token read keyed on the human connector (the switcher).
    get_tokens.assert_awaited_once_with(user_id="user_a", bound_to="org_1")
    # Auth file written under the container owner (org_1), not the connector.
    pre_stage.assert_awaited_once_with(user_id="org_1", oauth_tokens={"access_token": "tok", "account_id": "acc"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployed_tag", "expected_policy"),
    [
        ("2026.7.2-isol8.4-abc1234", None),
        ("2026.8.1-isol8.2-035a60f", {"allow": ["openai/*"]}),
    ],
)
async def test_hot_swap_gates_model_policy_on_the_running_image(monkeypatch, deployed_tag, expected_policy):
    """``modelPolicy`` is an 8.1-only key and ``agents.defaults`` is strict on
    7.2, so the hot swap must gate it on the image the owner's container is
    ACTUALLY running -- not the env pin, which a live container lags by a whole
    upstream version during a rollback or promotion window. Writing it from the
    pin would crash-loop a 7.2 container on its next config reload."""
    monkeypatch.setattr(
        "core.services.oauth_service.get_decrypted_tokens",
        AsyncMock(return_value={"access_token": "tok", "account_id": "acc"}),
    )
    monkeypatch.setattr("core.containers.workspace.pre_stage_auth_profile_store", AsyncMock())
    monkeypatch.setattr("core.containers.config.premium_model_ref", lambda *a, **k: None)
    monkeypatch.setattr("core.containers.config.cheap_model_ref", lambda *a, **k: "cheap")
    replace = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.replace_provider_block", replace)
    fake_manager = MagicMock()
    fake_manager._deployed_image_tag = AsyncMock(return_value=deployed_tag)
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)

    await billing_service._hot_swap_provider(
        owner_id="org_1",
        clerk_user_id="user_a",
        new_provider="chatgpt_oauth",
    )

    fake_manager._deployed_image_tag.assert_awaited_once_with("org_1")
    assert replace.await_args.kwargs["model_policy"] == expected_policy


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
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: fake_manager)
    # The cold path's takeover fence reads the container row lazily from the
    # real repo module - no row here, so the fence is skipped.
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.asyncio,
        "create_task",
        lambda coro: MagicMock(),
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
