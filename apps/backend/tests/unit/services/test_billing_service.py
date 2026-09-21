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
async def test_change_provider_takes_over_before_it_dispatches(monkeypatch):
    """ONE path: re-provision, which re-derives the owner's Bedrock AIPs and rewrites every agent
    profile from the committed provider_choice. Nothing is stopped or drained -- the provision is
    two S3 writes, and the agent reads the rewritten profile on its next turn.

    The takeover is the load-bearing half: ``dispatch_provision``'s claim does not admit a
    "running" row, so a bare dispatch would stand down SILENTLY while we told the caller
    ``restart: True`` and the owner kept running on the old provider.
    """
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "stripe_subscription_id": "sub_1"}),
    )
    # change_provider imports is_subscription_active LAZILY from
    # core.services.provision_gate, so patch it at the source module.
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"owner_id": "user_x"})
    )
    takeover = AsyncMock()
    monkeypatch.setattr("core.repositories.container_repo.takeover_provision", takeover)
    dispatch = MagicMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)
    created: list = []
    monkeypatch.setattr(billing_service.asyncio, "create_task", lambda coro: created.append(coro) or MagicMock())

    result = await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    assert result["restart"] is True
    takeover.assert_awaited_once_with("user_x")
    dispatch.assert_called_once_with("user_x")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_managed_switch_ensures_owner_key_before_provider_commit(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_ENABLED", True)
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

    async def dispatch(owner):
        pass

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)
    monkeypatch.setattr(billing_service.asyncio, "create_task", lambda coro: coro.close())

    await billing_service.change_provider(
        owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="openrouter_managed"
    )
    assert order == ["key", "commit"]


@pytest.mark.asyncio
async def test_managed_switch_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_ENABLED", False, raising=False)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    commit = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", commit)
    with pytest.raises(billing_service.ChangeProviderError, match="not enabled"):
        await billing_service.change_provider(
            owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="openrouter_managed"
        )
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_key_failure_keeps_provider_unchanged(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_ENABLED", True)
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
    monkeypatch.setattr(billing_service.settings, "OPENROUTER_MANAGED_ENABLED", True)
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
async def test_retry_of_committed_provider_with_pending_marker_dispatches_again(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"needs_provision": True})
    )
    monkeypatch.setattr("core.repositories.container_repo.takeover_provision", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

    async def dispatch(owner):
        pass

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)
    monkeypatch.setattr(billing_service.asyncio, "create_task", lambda coro: coro.close())

    result = await billing_service.change_provider(
        owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="bedrock_claude"
    )
    assert result["restart"] is True
    billing_service.billing_repo.set_provider_choice.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_refuses_different_choice_while_switch_pending(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda row: True)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"needs_provision": True})
    )
    commit = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", commit)
    monkeypatch.setattr(
        "core.services.key_service.get_secret_arn", AsyncMock(return_value="arn:aws:secretsmanager:key")
    )

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="org_a", clerk_user_id="user_a", owner_type="org", provider_choice="byo_key", byo_provider="openai"
        )
    assert exc.value.code == "provider_switch_pending"
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_provider_skips_the_takeover_for_a_workspace_with_no_row(monkeypatch):
    """With no row there is no lease to fence, and a takeover would CREATE one as
    "provisioning" with a fresh lease -- which the dispatch's own claim then refuses, breaking
    the create-from-scratch path for a never-provisioned owner."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "stripe_subscription_id": "sub_1"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    takeover = AsyncMock()
    monkeypatch.setattr("core.repositories.container_repo.takeover_provision", takeover)
    dispatch = MagicMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)
    created: list = []
    monkeypatch.setattr(billing_service.asyncio, "create_task", lambda coro: created.append(coro) or MagicMock())

    result = await billing_service.change_provider(
        owner_id="user_x",
        clerk_user_id="user_x",
        owner_type="personal",
        provider_choice="bedrock_claude",
    )

    assert result["restart"] is True
    takeover.assert_not_awaited()
    dispatch.assert_called_once_with("user_x")


@pytest.mark.asyncio
async def test_change_provider_reverts_the_commit_when_the_takeover_fails(monkeypatch):
    """The new provider is committed BEFORE the takeover, so a failed takeover leaves a row that
    matches the request -- and a retry would hit the no-op fast path and dispatch nothing. The
    commit is reverted so the retry re-runs the whole switch."""
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "subscription_status": "active",
                "stripe_subscription_id": "sub_1",
                "provider_choice": "byo_key",
                "byo_provider": "openai",
            }
        ),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    mark = {"generation": "gen-new", "previous_needs_provision": False, "previous_generation": "gen-old"}
    set_choice = AsyncMock(side_effect=[{"_switch_mark": mark}, {}])
    monkeypatch.setattr(billing_service.billing_repo, "set_provider_choice", set_choice)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr("core.services.key_service.KeyService", MagicMock())
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"owner_id": "user_x"})
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.takeover_provision", AsyncMock(side_effect=RuntimeError("ddb down"))
    )
    restore_marker = AsyncMock(return_value=True)
    monkeypatch.setattr("core.repositories.container_repo.restore_needs_provision", restore_marker)
    dispatch = MagicMock()
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)

    with pytest.raises(billing_service.ChangeProviderError) as exc:
        await billing_service.change_provider(
            owner_id="user_x",
            clerk_user_id="user_x",
            owner_type="personal",
            provider_choice="bedrock_claude",
        )

    assert exc.value.code == "reprovision_stop_failed"
    dispatch.assert_not_called()
    # Committed to bedrock, then restored to what the workspace is actually running.
    assert [c.kwargs["provider_choice"] for c in set_choice.await_args_list] == ["bedrock_claude", "byo_key"]
    assert set_choice.await_args_list[1].kwargs["requires_reprovision"] is False
    restore_marker.assert_awaited_once_with("user_x", mark)


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
