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
    assert "REMOVE" not in counter["UpdateExpression"]
    release = table.update_item.call_args.kwargs  # the claim is released after the transaction, only if still ours
    assert release["UpdateExpression"] == "REMOVE openCallId, openCallAt"
    assert release["ConditionExpression"] == "openCallId = :c"
    assert release["ExpressionAttributeValues"] == {":c": "call-1"}


@pytest.mark.asyncio
async def test_close_call_free_leaves_a_newer_calls_claim_intact():
    table, client, metronome, patches = _patches(seconds=45, call={**CALL, "billing": "free", "holdMicrocents": 0})
    claimed = {"Attributes": {**CALL, "billing": "free", "holdMicrocents": 0, "status": "call_closing"}}

    def update(**kw):
        if kw.get("ConditionExpression") == "openCallId = :c":
            raise _conditional_fail()  # openCallId now names a newer call
        return claimed

    table.update_item.side_effect = update
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"] == {":c": "call-1"}


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
    assert second["UpdateExpression"] == "SET usedSeconds = :s, #d = :day"


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


ROW_A = {"remaining_microcents": 900_000, "reserved_microcents": 0, "openrouter_reserved_microcents": 100}


def _hold_patches(snapshots, update):
    gate = MagicMock(update_item=update)
    return [
        patch.object(vc.voice_billing, "credit_snapshot", AsyncMock(side_effect=snapshots)),
        patch.object(vc.metronome_fast_gate, "_table", return_value=gate),
        patch.object(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2"),
    ]


@pytest.mark.asyncio
async def test_hold_race_loser_rereads_and_reserves_only_remaining_headroom():
    row_b = {**ROW_A, "openrouter_reserved_microcents": 100 + 500_000}  # the winner's hold landed
    update = MagicMock(side_effect=[_conditional_fail(), {}])
    patches = _hold_patches([(240, ROW_A), (60, row_b)], update)
    hold, budget = await _run(patches, vc._hold("org_a"))
    assert budget == 60 and hold == 60_000
    first, second = (c.kwargs for c in update.call_args_list)
    assert first["ExpressionAttributeValues"][":v2"] == 100
    assert second["ExpressionAttributeValues"][":v2"] == 100 + 500_000  # condition pinned to the fresh snapshot
    assert second["ExpressionAttributeValues"][":h"] == hold


@pytest.mark.asyncio
async def test_hold_loser_with_no_headroom_left_gets_zero():
    update = MagicMock(side_effect=[_conditional_fail()])
    assert await _run(_hold_patches([(240, ROW_A), (0, {})], update), vc._hold("org_a")) == (0, 0)


@pytest.mark.asyncio
async def test_hold_three_condition_failures_is_voice_busy_without_a_hold():
    update = MagicMock(side_effect=[_conditional_fail()] * 3)
    with pytest.raises(vc.HTTPException) as err:
        await _run(_hold_patches([(240, ROW_A)] * 3, update), vc._hold("org_a"))
    assert err.value.status_code == 409 and err.value.detail["code"] == "voice_busy"
    assert update.call_count == 3


@pytest.mark.asyncio
async def test_hold_row_without_reservation_fields_uses_attribute_not_exists():
    update = MagicMock(return_value={})
    row = {"remaining_microcents": 900_000}
    hold, budget = await _run(_hold_patches([(240, row)], update), vc._hold("org_a"))
    cond = update.call_args.kwargs["ConditionExpression"]
    assert "attribute_not_exists(reserved_microcents)" in cond
    assert "attribute_not_exists(openrouter_reserved_microcents)" in cond
    assert "remaining_microcents = :v0" in cond and budget == 240


@pytest.mark.asyncio
async def test_persist_failure_still_bills_known_seconds():
    table, client, metronome, patches = _patches(seconds=90)
    table.update_item.side_effect = [
        {"Attributes": {**CALL, "status": "call_closing"}},  # claim
        ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"),  # persist
    ]
    assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    assert metronome.ingest.await_args.args[0][0]["properties"]["seconds"] == "90"
    client.transact_write_items.assert_called_once()


@pytest.mark.asyncio
async def test_wipe_owner_voice_closes_and_deletes_only_this_owners_calls():
    table = MagicMock()
    mine = {**CALL, "callId": "a", "sessionId": "s_a"}
    other = {**CALL, "callId": "b", "ownerId": "org_b", "sessionId": "s_b"}
    close = AsyncMock()
    with (
        patch.object(vc, "_table", return_value=table),
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=[[mine, other], []])),
        patch.object(vc.openai_live, "close_session", close),
        patch.object(vc, "_stop_turn", AsyncMock()),
    ):
        await vc.wipe_owner_voice("org_a")
    close.assert_awaited_once_with("s_a")
    keys = [c.kwargs["Key"]["owner_id"] for c in table.delete_item.call_args_list]
    assert keys == ["__call__#a", "__voice__#org_a"]
