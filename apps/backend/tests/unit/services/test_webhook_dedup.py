"""Unit tests for the Stripe webhook event-dedup helper."""

import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.services.webhook_dedup import (
    MembershipLifecycleInProgress,
    WebhookProcessingInProgress,
    WebhookDedupResult,
    begin_membership_lifecycle,
    begin_event_processing,
    complete_membership_lifecycle,
    complete_event_processing,
    get_membership_lifecycle,
    record_event_or_skip,
    record_permanent_marker,
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
async def test_permanent_marker_has_no_ttl_and_is_idempotent(dedup_table, monkeypatch):
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", dedup_table)
    await record_permanent_marker("human-subject-deleted:abc", source="clerk-human-deleted")
    await record_permanent_marker("human-subject-deleted:abc", source="clerk-human-deleted")

    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(dedup_table)
        .get_item(Key={"event_id": "human-subject-deleted:abc"}, ConsistentRead=True)["Item"]
    )
    assert item["state"] == "completed"
    assert "ttl" not in item


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
    assert await get_membership_lifecycle(owner_id, alias) == {
        "event_id": "msg_create",
        "sequence": create_sequence,
        "state": "allowed",
    }
