"""Unit tests for the Stripe webhook event-dedup helper."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services.webhook_dedup import (
    MembershipLifecycleInProgress,
    TopUpIntentConflict,
    WebhookProcessingInProgress,
    WebhookDedupResult,
    begin_membership_lifecycle,
    begin_event_processing,
    complete_top_up_intent,
    complete_membership_lifecycle,
    complete_event_processing,
    get_or_create_top_up_intent,
    get_active_top_up,
    membership_lifecycle_key,
    record_event_or_skip,
    settle_active_top_up,
    set_active_top_up_status,
)


@pytest.fixture
def dedup_table():
    """Create a moto-mocked WEBHOOK_DEDUP_TABLE matching the CDK schema."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-webhook-event-dedup",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield "test-webhook-event-dedup"


@pytest.mark.asyncio
async def test_first_call_records_event(dedup_table, monkeypatch):
    """First call for a new event_id returns RECORDED."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    result = await record_event_or_skip("evt_abc123", source="stripe")
    assert result is WebhookDedupResult.RECORDED


@pytest.mark.asyncio
async def test_second_call_skips_event(dedup_table, monkeypatch):
    """Second call for the same event_id returns ALREADY_SEEN."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    first = await record_event_or_skip("evt_abc123", source="stripe")
    second = await record_event_or_skip("evt_abc123", source="stripe")
    assert first is WebhookDedupResult.RECORDED
    assert second is WebhookDedupResult.ALREADY_SEEN


@pytest.mark.asyncio
async def test_different_sources_share_namespace(dedup_table, monkeypatch):
    """event_id is global; the same id from different sources still dedupes."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    first = await record_event_or_skip("shared_id", source="stripe")
    second = await record_event_or_skip("shared_id", source="clerk")
    assert first is WebhookDedupResult.RECORDED
    assert second is WebhookDedupResult.ALREADY_SEEN


@pytest.mark.asyncio
async def test_recorded_item_has_30day_ttl(dedup_table, monkeypatch):
    """Items expire 30 days after creation via the table's TTL attribute."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    before = int(time.time())
    await record_event_or_skip("evt_ttl", source="stripe")
    after = int(time.time())

    client = boto3.client("dynamodb", region_name="us-east-1")
    item = client.get_item(TableName=dedup_table, Key={"event_id": {"S": "evt_ttl"}})["Item"]
    ttl = int(item["ttl"]["N"])
    assert before + 2_592_000 - 5 <= ttl <= after + 2_592_000 + 5


# ---------------------------------------------------------------------------
# Telemetry: dedup-decision metric + non-conditional ClientError log/re-raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_metric_recorded_on_first_call(dedup_table, monkeypatch):
    """RECORDED decision emits webhook.dedup{source, outcome=recorded}."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    with patch("core.observability.metrics.put_metric") as mock_put:
        result = await record_event_or_skip("evt_metric_1", source="stripe")

    assert result is WebhookDedupResult.RECORDED
    mock_put.assert_called_once_with("webhook.dedup", dimensions={"source": "stripe", "outcome": "recorded"})


@pytest.mark.asyncio
async def test_emits_metric_already_seen_on_replay(dedup_table, monkeypatch):
    """ALREADY_SEEN decision emits webhook.dedup{source, outcome=already_seen}."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    await record_event_or_skip("evt_metric_2", source="clerk")
    with patch("core.observability.metrics.put_metric") as mock_put:
        result = await record_event_or_skip("evt_metric_2", source="clerk")

    assert result is WebhookDedupResult.ALREADY_SEEN
    mock_put.assert_called_once_with("webhook.dedup", dimensions={"source": "clerk", "outcome": "already_seen"})


@pytest.mark.asyncio
async def test_metric_failure_does_not_break_decision(dedup_table, monkeypatch):
    """A metrics outage must never turn a successful dedup into a raise."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    with patch(
        "core.observability.metrics.put_metric",
        side_effect=RuntimeError("cloudwatch unavailable"),
    ):
        result = await record_event_or_skip("evt_metric_safe", source="stripe")

    assert result is WebhookDedupResult.RECORDED


@pytest.mark.asyncio
async def test_non_conditional_client_error_logs_and_reraises(monkeypatch, caplog):
    """A non-ConditionalCheckFailedException ClientError is logged (WARNING with
    event_id + source) and re-raised — never silently swallowed, no metric."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")

    boom = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
        "PutItem",
    )
    fake_table = MagicMock()
    fake_table.put_item.side_effect = boom

    with (
        patch("core.services.webhook_dedup._table", return_value=fake_table),
        patch("core.observability.metrics.put_metric") as mock_put,
        caplog.at_level("WARNING", logger="core.services.webhook_dedup"),
    ):
        with pytest.raises(ClientError) as exc_info:
            await record_event_or_skip("evt_throttled", source="stripe")

    assert exc_info.value is boom
    # No dedup-decision metric on the error path.
    mock_put.assert_not_called()
    # WARNING carries the event_id + source for diagnosis.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("evt_throttled" in m and "stripe" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_put_item_is_offloaded_to_a_thread(monkeypatch):
    """The admin idempotency claim (and every other caller) reaches this on the
    request-handling event loop -- the blocking boto3 PutItem must not run on
    it directly, exactly like the sibling `_claim_active_top_up` PutItem."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", "test-webhook-event-dedup")

    fake_table = MagicMock()

    async def fake_run_in_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch("core.services.webhook_dedup._table", return_value=fake_table),
        patch("core.services.webhook_dedup.run_in_thread", AsyncMock(side_effect=fake_run_in_thread)) as mock_run,
    ):
        result = await record_event_or_skip("evt_offloaded", source="admin")

    assert result is WebhookDedupResult.RECORDED
    mock_run.assert_awaited_once()
    # The offloaded call is the mocked table's put_item, with the same
    # conditional-write shape as before.
    assert mock_run.call_args.args[0] == fake_table.put_item
    assert mock_run.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(event_id)"
    fake_table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_processing_lease_retries_only_after_expiry_and_completed_events_stay_done(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    first = await begin_event_processing("msg_delete", source="clerk", lease_seconds=30)
    assert first is not None

    with pytest.raises(WebhookProcessingInProgress):
        await begin_event_processing("msg_delete", source="clerk", lease_seconds=30)

    with patch("core.services.webhook_dedup.time.time", return_value=time.time() + 31):
        replacement = await begin_event_processing("msg_delete", source="clerk", lease_seconds=30)
    assert replacement is not None and replacement != first

    with pytest.raises(RuntimeError, match="lease was lost"):
        await complete_event_processing("msg_delete", first)
    await complete_event_processing("msg_delete", replacement)
    assert await begin_event_processing("msg_delete", source="clerk", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_top_up_intent_pins_money_binding_and_rejects_a_changed_workspace_link(dedup_table, monkeypatch):
    """A retry must keep the original Metronome body, but must not replay it
    after the workspace's Stripe/Metronome binding changes."""
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    binding = {
        "amount_cents": 2500,
        "effective_at": "2026-09-10T00:00:00.000Z",
        "metronome_customer_id": "metro_1",
        "metronome_contract_id": "contract_1",
        "stripe_customer_id": "cus_1",
        "product_id": "prod_1",
        "credit_type_id": "credit_1",
        "description": "isol8 top-up owner=org_1 amount_cents=2500",
        "idempotency_key": "topup:org_1:11111111-1111-4111-8111-111111111111",
    }

    created = await get_or_create_top_up_intent("org_1", "11111111-1111-4111-8111-111111111111", binding)
    replay = await get_or_create_top_up_intent("org_1", "11111111-1111-4111-8111-111111111111", binding)

    assert created["state"] == replay["state"] == "pending"
    assert {key: replay[key] for key in binding} == binding
    await complete_top_up_intent(replay["event_id"])
    completed = await get_or_create_top_up_intent("org_1", "11111111-1111-4111-8111-111111111111", binding)
    assert completed["state"] == "completed"

    with pytest.raises(TopUpIntentConflict, match="binding changed"):
        await get_or_create_top_up_intent(
            "org_1",
            "11111111-1111-4111-8111-111111111111",
            {**binding, "stripe_customer_id": "cus_replaced"},
        )


@pytest.mark.asyncio
async def test_top_up_provider_attempt_timestamp_is_first_writer_wins(dedup_table, monkeypatch):
    """Concurrent/replayed requests share the first possible provider-write time."""
    from core.services import webhook_dedup

    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    request_id = "10111111-1111-4111-8111-111111111111"
    binding = {
        "amount_cents": 2500,
        "effective_at": "2026-09-10T00:00:00.000Z",
        "metronome_customer_id": "metro_1",
        "metronome_contract_id": "contract_1",
        "stripe_customer_id": "cus_1",
        "product_id": "prod_1",
        "credit_type_id": "credit_1",
        "description": "isol8 top-up owner=org_1 amount_cents=2500",
        "idempotency_key": request_id,
    }
    intent = await get_or_create_top_up_intent("org_1", request_id, binding)

    with patch("core.services.webhook_dedup.time.time", return_value=100):
        first = await webhook_dedup.record_top_up_provider_attempt(intent["event_id"])
    with patch("core.services.webhook_dedup.time.time", return_value=200):
        replay = await webhook_dedup.record_top_up_provider_attempt(intent["event_id"])

    assert first == replay == 100


@pytest.mark.asyncio
async def test_active_top_up_allows_same_request_and_replaces_only_after_terminal_settlement(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    first_request = "12111111-1111-4111-8111-111111111111"
    second_request = "13111111-1111-4111-8111-111111111111"
    binding = {
        "amount_cents": 2500,
        "effective_at": "2026-09-10T00:00:00.000Z",
        "metronome_customer_id": "metro_1",
        "metronome_contract_id": "contract_1",
        "stripe_customer_id": "cus_1",
        "product_id": "prod_1",
        "credit_type_id": "credit_1",
        "description": "isol8 top-up owner=org_1 amount_cents=2500",
        "idempotency_key": first_request,
    }
    first = await get_or_create_top_up_intent("org_1", first_request, binding)
    assert (await get_or_create_top_up_intent("org_1", first_request, binding))["event_id"] == first["event_id"]

    with pytest.raises(TopUpIntentConflict, match="active"):
        await get_or_create_top_up_intent("org_1", second_request, {**binding, "idempotency_key": second_request})

    assert await settle_active_top_up("org_1", "metro_1", "contract_1", "paid", invoice_id="invoice_1") is True
    assert (await get_active_top_up("org_1"))["status"] == "paid"
    assert (await get_or_create_top_up_intent("org_1", first_request, binding))["state"] == "paid"
    second = await get_or_create_top_up_intent("org_1", second_request, {**binding, "idempotency_key": second_request})
    assert second["request_id"] == second_request


@pytest.mark.asyncio
async def test_active_top_up_rejects_customer_or_contract_mismatch_and_records_failure(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    request_id = "14111111-1111-4111-8111-111111111111"
    binding = {
        "amount_cents": 2500,
        "effective_at": "2026-09-10T00:00:00.000Z",
        "metronome_customer_id": "metro_1",
        "metronome_contract_id": "contract_1",
        "stripe_customer_id": "cus_1",
        "product_id": "prod_1",
        "credit_type_id": "credit_1",
        "description": "isol8 top-up owner=org_1 amount_cents=2500",
        "idempotency_key": request_id,
    }
    await get_or_create_top_up_intent("org_1", request_id, binding)
    assert await settle_active_top_up("org_1", "metro_other", "contract_1", "failed", invoice_id="invoice_1") is False
    assert await settle_active_top_up("org_1", "metro_1", "contract_other", "failed", invoice_id="invoice_1") is False
    assert await settle_active_top_up("org_1", "metro_1", "contract_1", "failed", invoice_id="invoice_1") is True
    assert (await get_active_top_up("org_1"))["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "failed"])
async def test_late_acceptance_preserves_terminal_intent(dedup_table, monkeypatch, status):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(dedup_table)
    table.put_item(Item={"event_id": "intent_A", "kind": "metronome-top-up", "state": status})
    await complete_top_up_intent("intent_A")
    assert table.get_item(Key={"event_id": "intent_A"})["Item"]["state"] == status


@pytest.mark.asyncio
async def test_old_request_acknowledgement_cannot_advance_new_pointer(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(dedup_table)
    table.put_item(
        Item={
            "event_id": "metronome-top-up-active:org_1",
            "kind": "metronome-top-up-active",
            "owner_id": "org_1",
            "intent_event_id": "intent_B",
            "status": "pending",
            "metronome_customer_id": "metro_1",
            "metronome_contract_id": "contract_1",
        }
    )
    assert (
        await set_active_top_up_status("org_1", "metro_1", "contract_1", "requested", intent_event_id="intent_A")
        is False
    )
    assert (await get_active_top_up("org_1"))["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "failed"])
@pytest.mark.parametrize("fail_write", [None, "binding", 1, 2, 3])
async def test_invoice_replay_finishes_only_its_bound_intent(dedup_table, monkeypatch, status, fail_write):
    from core.services import webhook_dedup

    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    binding = {
        "amount_cents": 2500,
        "effective_at": "2026-09-10T00:00:00.000Z",
        "metronome_customer_id": "metro_1",
        "metronome_contract_id": "contract_1",
        "stripe_customer_id": "cus_1",
        "product_id": "prod_1",
        "credit_type_id": "credit_1",
        "description": "isol8 top-up owner=org_1 amount_cents=2500",
        "idempotency_key": "request_1",
    }
    first = await get_or_create_top_up_intent("org_1", "request_1", binding)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(dedup_table)
    if fail_write:
        update = table.update_item
        put = table.put_item
        writes = 0

        def interrupted_put(**kwargs):
            result = put(**kwargs)
            if fail_write == "binding":
                raise RuntimeError("response lost after durable write")
            return result

        def interrupted_update(**kwargs):
            nonlocal writes
            writes += 1
            result = update(**kwargs)
            if writes == fail_write:
                raise RuntimeError("response lost after durable write")
            return result

        with (
            patch.object(webhook_dedup, "_table", return_value=table),
            patch.object(table, "update_item", side_effect=interrupted_update),
            patch.object(table, "put_item", side_effect=interrupted_put),
        ):
            with pytest.raises(RuntimeError, match="response lost"):
                await settle_active_top_up("org_1", "metro_1", "contract_1", status, invoice_id="invoice_1")

    # If a partial settlement already released the pointer, a new purchase can
    # start before the old invoice is retried.
    if (await get_active_top_up("org_1"))["status"] in {"paid", "failed"}:
        await get_or_create_top_up_intent("org_1", "request_2", {**binding, "idempotency_key": "request_2"})
    await settle_active_top_up("org_1", "metro_1", "contract_1", status, invoice_id="invoice_1")
    await complete_top_up_intent(first["event_id"])
    assert table.get_item(Key={"event_id": first["event_id"]})["Item"]["state"] == status
    second = await get_or_create_top_up_intent("org_1", "request_2", {**binding, "idempotency_key": "request_2"})
    # A different event ID (including a contrary status) for the same invoice
    # must be a permanent no-op even after the active pointer changes.
    assert await settle_active_top_up("org_1", "metro_1", "contract_1", "paid", invoice_id="invoice_1") is False
    assert await settle_active_top_up("org_1", "metro_1", "contract_1", "failed", invoice_id="invoice_1") is False
    assert (await get_active_top_up("org_1"))["request_id"] == "request_2"
    assert (await get_active_top_up("org_1"))["status"] == "pending"
    assert table.get_item(Key={"event_id": second["event_id"]})["Item"]["state"] == "pending"
    assert (await get_or_create_top_up_intent("org_1", "request_1", binding))["state"] == status
    assert (await get_active_top_up("org_1"))["request_id"] == "request_2"
    marker = table.get_item(Key={"event_id": "metronome-top-up-invoice:invoice_1"})["Item"]
    assert marker["intent_event_id"] == first["event_id"]
    assert marker["state"] == "completed"
    assert "ttl" not in marker


@pytest.mark.asyncio
async def test_container_independent_membership_epoch_serializes_and_rejects_stale_events(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    owner_id = "org_without_container"
    alias = "clerk-" + "a" * 64 + "@identity.lightbulb.invalid"
    delete_sequence = "00000000000000000001:1:msg_delete"
    create_sequence = "00000000000000000002:0:msg_create"

    delete_claim = await begin_membership_lifecycle(
        owner_id,
        alias,
        event_id="msg_delete",
        sequence=delete_sequence,
        access_state="revoked",
    )
    assert delete_claim is not None
    with pytest.raises(MembershipLifecycleInProgress):
        await begin_membership_lifecycle(
            owner_id,
            alias,
            event_id="msg_create",
            sequence=create_sequence,
            access_state="allowed",
        )

    await complete_membership_lifecycle(owner_id, alias, delete_claim)
    create_claim = await begin_membership_lifecycle(
        owner_id,
        alias,
        event_id="msg_create",
        sequence=create_sequence,
        access_state="allowed",
    )
    assert create_claim is not None
    await complete_membership_lifecycle(owner_id, alias, create_claim)

    assert (
        await begin_membership_lifecycle(
            owner_id,
            alias,
            event_id="msg_delete_late",
            sequence=delete_sequence,
            access_state="revoked",
        )
        is None
    )
    stored = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(dedup_table)
        .get_item(Key={"event_id": membership_lifecycle_key(owner_id, alias)}, ConsistentRead=True)["Item"]
    )
    assert stored["lifecycle_event_id"] == "msg_create"
    assert stored["sequence"] == create_sequence
    assert stored["access_state"] == "allowed"
