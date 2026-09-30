import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services import voice_calls as vc

CALL = {
    "owner_id": "__call__#call-1",
    "callId": "call-1",
    "ownerId": "org_a",
    "userId": "user_a",
    "agentId": "agent-1",
    "companyId": "co-1",
    "sessionId": "live_1",
    "startedAt": 1000,
    "deadline": 1600,
    "billing": "credits",
    "holdMicrocents": 36_000,
    "status": "call_open",
    "turnRunId": None,
}


def _conditional_fail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def _patches(*, claim_ok=True, seconds=90, call=CALL):
    table = MagicMock()
    table.get_item.return_value = {"Item": dict(call)}
    if claim_ok:
        table.update_item.return_value = {"Attributes": {**call, "status": "call_closing"}}
    else:
        table.update_item.side_effect = _conditional_fail()
    table.name = "lightbulb-dev-containers"
    client = MagicMock()
    table.meta.client = client
    metronome = MagicMock(ingest=AsyncMock())
    return (
        table,
        client,
        metronome,
        [
            patch.object(vc, "_table", return_value=table),
            patch.object(vc.openai_live, "close_session", AsyncMock(return_value=seconds)),
            patch.object(vc, "_metronome", return_value=metronome),
            patch.object(vc, "_fast_gate_table_name", return_value="lightbulb-dev-metronome-fast-gate"),
            patch.object(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2"),
            patch.object(vc, "_stop_turn", AsyncMock()),
        ],
    )


async def _run(patches, coro):
    for p in patches:
        p.start()
    try:
        return await coro
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_close_call_bills_credits_once_and_deletes_row():
    table, client, metronome, patches = _patches(seconds=90)
    assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    event = metronome.ingest.await_args.args[0][0]
    assert event["event_type"] == "voice_call"
    assert event["transaction_id"] == "call-1"
    assert event["customer_id"] == "org_a"
    assert event["properties"]["provider_cost_nanodollars"] == "75000000"  # 90 s × $0.05/min
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    gate = items[0]["Update"]
    assert gate["TableName"] == "lightbulb-dev-metronome-fast-gate"
    assert gate["ExpressionAttributeValues"][":debit"] == {"N": "-90000"}
    assert gate["ExpressionAttributeValues"][":hold"] == {"N": "36000"}
    assert items[1]["Delete"]["Key"] == {"owner_id": {"S": "__call__#call-1"}}


@pytest.mark.asyncio
async def test_close_call_loser_does_nothing():
    table, client, metronome, patches = _patches(claim_ok=False)
    assert await _run(patches, vc.close_call("call-1", now=1100)) is False
    metronome.ingest.assert_not_awaited()
    client.transact_write_items.assert_not_called()


@pytest.mark.asyncio
async def test_close_call_attach_failure_bills_fallback():
    table, client, metronome, patches = _patches(seconds=None)
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    metric.assert_any_call("voice.usage.fallback")
    event = metronome.ingest.await_args.args[0][0]
    assert event["properties"]["seconds"] == "100"  # min(now, deadline) − startedAt


@pytest.mark.asyncio
async def test_close_call_free_counts_seconds_in_one_transaction():
    table, client, metronome, patches = _patches(seconds=45, call={**CALL, "billing": "free", "holdMicrocents": 0})
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        await _run(patches, vc.close_call("call-1", now=1100))
    metronome.ingest.assert_not_awaited()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    counter = items[0]["Update"]
    assert counter["Key"] == {"owner_id": {"S": "__voice__#org_a"}}
    assert counter["ExpressionAttributeValues"][":s"] == {"N": "45"}
    assert items[1]["Delete"]["Key"] == {"owner_id": {"S": "__call__#call-1"}}
    assert counter["UpdateExpression"].endswith("REMOVE openCallId")


@pytest.mark.asyncio
async def test_close_call_fires_stop_turn_in_background():
    table, client, metronome, patches = _patches(seconds=90)
    gate = asyncio.Event()

    async def slow_stop(call):
        await gate.wait()

    patches[-1] = patch.object(vc, "_stop_turn", slow_stop)
    try:
        # would hang forever if close_call awaited the stop
        assert await asyncio.wait_for(_run(patches, vc.close_call("call-1", now=1100)), 2) is True
    finally:
        gate.set()
        await asyncio.gather(*vc._background)


def _cancel(*codes):
    return ClientError(
        {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": [{"Code": c} for c in codes]},
        "TransactWriteItems",
    )


@pytest.mark.asyncio
async def test_close_call_free_day_rollover_retries_with_reset():
    table, client, metronome, patches = _patches(seconds=45, call={**CALL, "billing": "free", "holdMicrocents": 0})
    client.transact_write_items.side_effect = [_cancel("ConditionalCheckFailed", "None"), None]
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    second = client.transact_write_items.call_args.kwargs["TransactItems"][0]["Update"]
    assert second["UpdateExpression"] == "SET usedSeconds = :s, #d = :day REMOVE openCallId"


@pytest.mark.asyncio
async def test_close_call_credits_gate_skipped_still_deletes_row():
    table, client, metronome, patches = _patches(seconds=90)
    client.transact_write_items.side_effect = [_cancel("ConditionalCheckFailed", "None"), None]
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    metric.assert_any_call("voice.bill.gate_skipped")
    retry = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(retry) == 1 and "Delete" in retry[0]


@pytest.mark.asyncio
async def test_close_call_reraises_when_delete_condition_failed():
    table, client, metronome, patches = _patches(seconds=90)
    client.transact_write_items.side_effect = _cancel("None", "ConditionalCheckFailed")
    with pytest.raises(ClientError):
        await _run(patches, vc.close_call("call-1", now=1100))


@pytest.mark.asyncio
async def test_close_call_reclaim_bills_stored_usage_seconds():
    table, client, metronome, patches = _patches(seconds=None, call={**CALL, "usageSeconds": 90})
    table.update_item.return_value = {"Attributes": {**CALL, "usageSeconds": 90, "status": "call_closing"}}
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=5000)) is True
    event = metronome.ingest.await_args.args[0][0]
    assert event["properties"]["seconds"] == "90"
    assert ("voice.usage.fallback",) not in [c.args for c in metric.call_args_list]
