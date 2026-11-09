from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime

import httpx
import pytest

from core.billing import voice_plan, voice_billing


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "METRONOME_VOICE_ENROLLMENT_ENABLED": True,
        "METRONOME_API_KEY": "key",
        "METRONOME_VOICE_RATE_CARD_ID": "voice-card",
        "METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID": "voice-sub",
        "METRONOME_VOICE_METRIC_ID": "voice-metric",
        "STRIPE_SECRET_KEY": "sk_test_fake",
    }.items():
        monkeypatch.setattr(voice_plan.settings, key, value)
    client = MagicMock()
    client.list_contracts = AsyncMock(return_value=[])
    client.create_voice_contract = AsyncMock(return_value="voice-contract")
    client.get_contract = AsyncMock(return_value={"starting_at": "2026-11-08T00:00:00Z"})
    account = {
        "stripe_customer_id": "cus-1",
        "metronome_customer_id": "mc-1",
        "metronome_contract_id": "credits-contract",
        "owner_id": "org-1",
        "voice_plan_accepted_version": "voice-agents-v1",
        "voice_plan_accepted_by": "user-1",
    }

    async def save_plan(_owner, **fields):
        account.update(metronome_voice_contract_id=fields["contract_id"], voice_plan_status="pending_payment")

    with (
        patch.object(voice_plan.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="token")),
        patch.object(voice_plan.container_repo, "release_owner_lifecycle_claim", AsyncMock()),
        patch.object(voice_plan.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_plan.billing_repo, "set_voice_plan", AsyncMock(side_effect=save_plan)) as save,
        patch.object(voice_plan.voice_plan_payments, "default_card", AsyncMock(return_value=True)),
        patch.object(voice_plan.voice_plan_payments, "reconcile", AsyncMock(side_effect=lambda *_a, **_k: account)),
        patch.object(voice_plan.voice_plan_payments, "refresh", AsyncMock()),
        patch.object(voice_plan.metronome_provisioning, "_maybe_link_stripe", AsyncMock(return_value=True)) as link,
    ):
        yield account, client, save, link


async def test_explicit_enrollment_preserves_credit_contract_and_records_scoped_entitlement(env):
    account, client, save, link = env
    assert await voice_plan.enroll("org-1", client=client) == {
        "status": "pending_payment",
        "planVersion": "voice-agents-v1",
    }
    key = "lightbulb-voice:mc-1:voice-agents-v1"
    client.create_voice_contract.assert_awaited_once_with(
        customer_id="mc-1",
        rate_card_id="voice-card",
        subscription_product_id="voice-sub",
        uniqueness_key=key,
    )
    save.assert_awaited_once_with(
        "org-1", customer_id="mc-1", contract_id="voice-contract", usage_key=key, started_at="2026-11-08T00:00:00Z"
    )
    assert account["metronome_contract_id"] == "credits-contract"
    link.assert_awaited_once()


async def test_disabled_enrollment_does_not_make_provider_calls(env, monkeypatch):
    _, client, save, _ = env
    monkeypatch.setattr(voice_plan.settings, "METRONOME_VOICE_ENROLLMENT_ENABLED", False)
    with pytest.raises(voice_plan.VoicePlanError, match="unavailable"):
        await voice_plan.enroll("org-1", client=client)
    client.create_voice_contract.assert_not_awaited()
    save.assert_not_awaited()


async def test_stripe_link_failure_cannot_create_a_paid_contract(env):
    _, client, save, link = env
    link.return_value = False
    with pytest.raises(voice_plan.VoicePlanError, match="payment_setup_required"):
        await voice_plan.enroll("org-1", client=client)
    client.create_voice_contract.assert_not_awaited()
    save.assert_not_awaited()


async def test_existing_entitlement_is_idempotent(env):
    account, client, save, link = env
    account.update(
        voice_plan_status="active",
        metronome_voice_contract_id="voice-contract",
        voice_plan_paid_through="2099-01-01T00:00:00Z",
    )
    await voice_plan.enroll("org-1", client=client)
    client.create_voice_contract.assert_not_awaited()
    link.assert_not_awaited()
    save.assert_not_awaited()


async def test_crash_after_provider_create_adopts_only_exact_enrollment_key(env):
    _, client, save, _ = env
    client.list_contracts.return_value = [
        {"id": "unrelated", "rate_card_id": "voice-card"},
        {"id": "winner", "uniqueness_key": "lightbulb-voice:mc-1:voice-agents-v1"},
    ]
    await voice_plan.enroll("org-1", client=client)
    client.create_voice_contract.assert_not_awaited()
    assert save.await_args.kwargs["contract_id"] == "winner"


async def test_concurrent_create_409_adopts_winner(env):
    _, client, save, _ = env
    client.list_contracts.side_effect = [
        [],
        [{"id": "winner", "uniqueness_key": "lightbulb-voice:mc-1:voice-agents-v1"}],
    ]
    client.create_voice_contract.side_effect = httpx.HTTPStatusError(
        "conflict",
        request=httpx.Request("POST", "https://api.test"),
        response=httpx.Response(409),
    )
    await voice_plan.enroll("org-1", client=client)
    assert save.await_args.kwargs["contract_id"] == "winner"


def test_voice_entitlement_selects_plan_without_legacy_credit_or_trial_billing():
    account = {
        "voice_plan_status": "active",
        "metronome_voice_contract_id": "voice-contract",
        "voice_plan_paid_through": "2099-01-01T00:00:00Z",
        "voice_plan_paid_periods": {"fee": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}},
    }
    assert voice_billing.billing_mode(account) == "plan"
    assert voice_billing.billing_mode({**account, "voice_plan_status": "canceled"}) is None
    assert (
        voice_billing.billing_mode(
            {**account, "voice_plan_status": "canceling", "voice_plan_ending_at": "2099-01-01T00:00:00Z"}
        )
        == "plan"
    )
    assert (
        voice_billing.billing_mode(
            {**account, "voice_plan_status": "canceling", "voice_plan_ending_at": "2000-01-01T00:00:00Z"}
        )
        is None
    )


async def test_reported_minutes_freezes_plan_scope_and_enqueues_durable_event(env):
    account, _, _, _ = env
    account.update(
        voice_plan_status="active", metronome_voice_contract_id="voice-contract", voice_plan_usage_key="enrollment-key"
    )
    with (
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_billing.messaging_repo, "put_voice_usage_event", AsyncMock()) as outbox,
    ):
        await voice_billing.report_minutes("org-1", "meeting-1", "2026-11-08T12:00:00+00:00", 90, "meeting")
    outbox.assert_awaited_once()
    owner, transaction_id, event = outbox.await_args.args
    assert (owner, transaction_id) == ("org-1", "meeting-1")
    assert event["properties"] == {"seconds": "90", "kind": "meeting", "voice_plan": "enrollment-key"}
    assert event["customerId"] == "org-1"
    assert event["contractId"] == "voice-contract"
    assert event["usageKey"] == "enrollment-key"
    assert event["periodStart"] == "2026-11-01T00:00:00+00:00"
    assert event["periodEnd"] == "2026-12-01T00:00:00+00:00"


async def test_paid_usage_source_is_persisted_when_provider_credential_is_temporarily_missing(env, monkeypatch):
    account, _, _, _ = env
    account.update(
        voice_plan_status="active", metronome_voice_contract_id="voice-contract", voice_plan_usage_key="enrollment-key"
    )
    monkeypatch.setattr(voice_billing.settings, "METRONOME_API_KEY", "")
    with (
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_billing.messaging_repo, "put_voice_usage_event", AsyncMock()) as outbox,
    ):
        await voice_billing.report_minutes("org-1", "call-1", "2026-11-08T12:00:00Z", 90, "phone")
    outbox.assert_awaited_once()
    assert outbox.await_args.args[2]["contractId"] == "voice-contract"


async def test_voice_usage_delivery_retries_provider_before_scheduling_notice(env):
    event = {
        "transactionId": "phone-1",
        "customerId": "org-1",
        "timestamp": "2026-11-08T12:00:00Z",
        "seconds": 90,
        "kind": "phone",
        "properties": {"seconds": "90", "kind": "phone", "voice_plan": "enrollment-key"},
        "contractId": "voice-contract",
        "usageKey": "enrollment-key",
        "periodStart": "2026-11-01T00:00:00+00:00",
        "periodEnd": "2026-12-01T00:00:00+00:00",
    }
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock(ingest=AsyncMock())
    from core.temporal import client as temporal_client

    with (
        patch.object(
            voice_billing.messaging_repo, "_get", AsyncMock(return_value={"ownerId": "org-1", "voiceUsageEvent": event})
        ),
        patch.object(voice_billing.messaging_repo, "mark_voice_usage_ingested", AsyncMock()) as mark,
        patch.object(voice_billing.httpx, "AsyncClient", return_value=http),
        patch.object(voice_billing.metronome_client, "for_settings", return_value=client),
        patch.object(temporal_client, "start_voice_usage_notice", AsyncMock()) as start_notice,
    ):
        assert await voice_billing.deliver_usage_event("org-1", "phone-1") == "delivered"
    client.ingest.assert_awaited_once_with(
        [
            {
                "transaction_id": "phone-1",
                "customer_id": "org-1",
                "event_type": "voice_minutes",
                "timestamp": "2026-11-08T12:00:00Z",
                "properties": event["properties"],
            }
        ]
    )
    mark.assert_awaited_once_with("org-1", "phone-1")
    start_notice.assert_awaited_once()
    assert start_notice.await_args.args[0].period_start == event["periodStart"]


async def test_voice_usage_provider_timeout_leaves_queue_item_retryable(env):
    event = {
        "transactionId": "meeting-1",
        "customerId": "org-1",
        "timestamp": "2026-11-08T12:00:00Z",
        "properties": {"seconds": "90", "kind": "meeting"},
    }
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock(ingest=AsyncMock(side_effect=httpx.TimeoutException("timeout")))
    with (
        patch.object(
            voice_billing.messaging_repo, "_get", AsyncMock(return_value={"ownerId": "org-1", "voiceUsageEvent": event})
        ),
        patch.object(voice_billing.messaging_repo, "mark_voice_usage_ingested", AsyncMock()) as mark,
        patch.object(voice_billing.httpx, "AsyncClient", return_value=http),
        patch.object(voice_billing.metronome_client, "for_settings", return_value=client),
    ):
        with pytest.raises(httpx.TimeoutException):
            await voice_billing.deliver_usage_event("org-1", "meeting-1")
    mark.assert_not_awaited()


async def test_voice_usage_delivery_rejects_cross_tenant_receipt(env):
    with (
        patch.object(
            voice_billing.messaging_repo,
            "_get",
            AsyncMock(
                return_value={
                    "ownerId": "org-other",
                    "voiceUsageEvent": {"transactionId": "x", "customerId": "org-other"},
                }
            ),
        ),
        patch.object(voice_billing.httpx, "AsyncClient") as provider,
    ):
        with pytest.raises(RuntimeError, match="matching immutable event"):
            await voice_billing.deliver_usage_event("org-1", "x")
    provider.assert_not_called()


async def test_report_minutes_surfaces_outbox_write_failure(env):
    account, _, _, _ = env
    with (
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_billing.messaging_repo, "put_voice_usage_event", AsyncMock(side_effect=RuntimeError("ddb"))),
        patch.object(voice_billing.httpx, "AsyncClient") as provider,
    ):
        with pytest.raises(RuntimeError, match="ddb"):
            await voice_billing.report_minutes("org-1", "phone-1", "2026-11-08T12:00:00Z", 90, "phone")
    provider.assert_not_called()


async def test_usage_can_read_the_frozen_period_after_month_rollover(env):
    account, _, _, _ = env
    account.update(
        metronome_customer_id="mc-1",
        metronome_voice_contract_id="voice-contract",
        voice_plan_usage_key="enrollment-key",
        voice_plan_started_at="2026-09-15T00:00:00Z",
        voice_plan_ending_at="2026-12-01T00:00:00Z",
    )
    client = MagicMock(scoped_voice_seconds=AsyncMock(return_value=72_000))
    result = await voice_plan.usage(account, client=client, at=datetime(2026, 10, 18, tzinfo=UTC))
    assert result["periodStart"] == "2026-10-01T00:00:00+00:00"
    assert result["periodEnd"] == "2026-11-01T00:00:00+00:00"
    client.scoped_voice_seconds.assert_awaited_once_with(
        customer_id="mc-1",
        metric_id="voice-metric",
        usage_key="enrollment-key",
        starting_on="2026-10-01T00:00:00Z",
        ending_before="2026-11-01T00:00:00Z",
    )


async def test_cancellation_is_provider_first_and_reuses_frozen_end_timestamp(env, monkeypatch):
    account, client, _, _ = env
    account.update(voice_plan_status="active", metronome_voice_contract_id="voice-contract")
    client.end_voice_contract = AsyncMock()
    begin, finish = AsyncMock(return_value="2026-11-08T12:00:00Z"), AsyncMock()
    monkeypatch.setattr(voice_plan.billing_repo, "begin_voice_plan_cancellation", begin)
    monkeypatch.setattr(voice_plan.billing_repo, "finish_voice_plan_cancellation", finish)
    assert (await voice_plan.cancel("org-1", client=client))["status"] == "canceling"
    client.end_voice_contract.assert_awaited_once_with(
        customer_id="mc-1", contract_id="voice-contract", ending_before="2026-11-08T12:00:00Z"
    )
    finish.assert_awaited_once_with("org-1")
    assert account["metronome_contract_id"] == "credits-contract"


async def test_failed_provider_cancellation_keeps_local_entitlement_retryable(env, monkeypatch):
    account, client, _, _ = env
    account.update(voice_plan_status="active", metronome_voice_contract_id="voice-contract")
    client.end_voice_contract = AsyncMock(side_effect=httpx.ConnectError("offline"))
    monkeypatch.setattr(
        voice_plan.billing_repo, "begin_voice_plan_cancellation", AsyncMock(return_value="2026-11-08T12:00:00Z")
    )
    finish = AsyncMock()
    monkeypatch.setattr(voice_plan.billing_repo, "finish_voice_plan_cancellation", finish)
    with pytest.raises(httpx.ConnectError):
        await voice_plan.cancel("org-1", client=client)
    finish.assert_not_awaited()


async def test_ended_contract_cannot_be_silently_reactivated_without_a_new_subscription(env):
    account, client, save, _ = env
    account.update(voice_plan_status="canceled", metronome_voice_contract_id="old-contract")
    with pytest.raises(voice_plan.VoicePlanError, match="voice_plan_ended"):
        await voice_plan.enroll("org-1", client=client)
    save.assert_not_awaited()


async def test_enrollment_and_cancellation_share_a_lease_and_release_it_on_provider_failure(env):
    _, client, _, _ = env
    client.create_voice_contract.side_effect = httpx.ConnectError("offline")
    with (
        patch.object(voice_plan.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="token")) as claim,
        patch.object(voice_plan.container_repo, "release_owner_lifecycle_claim", AsyncMock()) as release,
    ):
        with pytest.raises(httpx.ConnectError):
            await voice_plan.enroll("org-1", client=client)
    claim.assert_awaited_once_with("voice-plan#org-1", stale_s=180)
    release.assert_awaited_once_with("voice-plan#org-1", token="token")


async def test_busy_plan_operation_cannot_create_or_cancel_any_provider_contract(env):
    _, client, save, _ = env
    with patch.object(voice_plan.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value=None)):
        with pytest.raises(voice_plan.VoicePlanError, match="voice_plan_busy"):
            await voice_plan.enroll("org-1", client=client)
        with pytest.raises(voice_plan.VoicePlanError, match="voice_plan_busy"):
            await voice_plan.cancel("org-1", client=client)
    client.create_voice_contract.assert_not_awaited()
    save.assert_not_awaited()
