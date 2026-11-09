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
    "kind": "huddle",
    "roomId": "room-1",
    "sessionId": None,
    "startedAt": 1000,
    "deadline": 1600,
    "billing": "credits",
    "holdMicrocents": 36_000,
    "status": "call_open",
    "turns": {},
}


def _conditional_fail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def _patches(*, claim_ok=True, call=CALL):
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
            patch.object(vc, "_metronome", return_value=metronome),
            patch.object(vc, "_fast_gate_table_name", return_value="lightbulb-dev-metronome-fast-gate"),
            patch.object(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2"),
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
    table, client, metronome, patches = _patches()
    assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=90)) is True
    event = metronome.ingest.await_args.args[0][0]
    assert event["event_type"] == "voice_call"
    assert event["transaction_id"] == "call-1"
    assert event["customer_id"] == "org_a"
    assert event["properties"]["provider_cost_nanodollars"] == "75000000"  # 90 s × $0.05/min
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    gate = items[0]["Update"]
    assert gate["TableName"] == "lightbulb-dev-metronome-fast-gate"
    assert gate["ExpressionAttributeValues"][":debit"] == -90000
    assert gate["ExpressionAttributeValues"][":hold"] == 36000
    assert items[1]["Delete"]["Key"] == {"owner_id": "__call__#call-1"}


async def test_close_plan_window_never_stacks_provider_credit_or_free_counter_debits():
    _, client, metronome, patches = _patches(call={**CALL, "billing": "plan", "holdMicrocents": 0})
    assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=90)) is True
    metronome.ingest.assert_not_awaited()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(items) == 1 and items[0]["Delete"]["Key"] == {"owner_id": "__call__#call-1"}


@pytest.mark.asyncio
async def test_close_call_loser_does_nothing():
    table, client, metronome, patches = _patches(claim_ok=False)
    assert await _run(patches, vc.close_call("call-1", now=1100)) is False
    metronome.ingest.assert_not_awaited()
    client.transact_write_items.assert_not_called()


@pytest.mark.asyncio
async def test_close_call_without_reported_usage_bills_zero():
    table, client, metronome, patches = _patches()
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    metric.assert_any_call("voice.huddle.usage_missing")
    assert metronome.ingest.await_args.args[0][0]["properties"]["seconds"] == "0"


@pytest.mark.asyncio
async def test_close_call_free_counts_seconds_in_one_transaction():
    table, client, metronome, patches = _patches(call={**CALL, "billing": "free", "holdMicrocents": 0})
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=45))
    metronome.ingest.assert_not_awaited()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    counter = items[0]["Update"]
    assert counter["Key"] == {"owner_id": "__voice__#org_a"}
    assert counter["ExpressionAttributeValues"][":s"] == 45
    assert items[1]["Delete"]["Key"] == {"owner_id": "__call__#call-1"}
    assert "REMOVE" not in counter["UpdateExpression"]
    release = table.update_item.call_args.kwargs  # the claim is released after the transaction, only if still ours
    assert release["UpdateExpression"] == "REMOVE openCallId, openCallUntil"
    assert release["ConditionExpression"] == "openCallId = :c"
    assert release["ExpressionAttributeValues"] == {":c": "call-1"}


@pytest.mark.asyncio
async def test_close_call_free_leaves_a_newer_calls_claim_intact():
    table, client, metronome, patches = _patches(call={**CALL, "billing": "free", "holdMicrocents": 0})
    claimed = {"Attributes": {**CALL, "billing": "free", "holdMicrocents": 0, "status": "call_closing"}}

    def update(**kw):
        if kw.get("ConditionExpression") == "openCallId = :c":
            raise _conditional_fail()  # openCallId now names a newer call
        return claimed

    table.update_item.side_effect = update
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=45)) is True
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"] == {":c": "call-1"}


def _cancel(*codes):
    return ClientError(
        {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": [{"Code": c} for c in codes]},
        "TransactWriteItems",
    )


@pytest.mark.asyncio
async def test_close_call_free_day_rollover_retries_with_reset():
    table, client, metronome, patches = _patches(call={**CALL, "billing": "free", "holdMicrocents": 0})
    client.transact_write_items.side_effect = [_cancel("ConditionalCheckFailed", "None"), None]
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=45)) is True
    second = client.transact_write_items.call_args.kwargs["TransactItems"][0]["Update"]
    assert second["UpdateExpression"] == "SET usedSeconds = :s, #d = :day"


@pytest.mark.asyncio
async def test_close_call_credits_gate_skipped_still_deletes_row():
    table, client, metronome, patches = _patches()
    client.transact_write_items.side_effect = [_cancel("ConditionalCheckFailed", "None"), None]
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=90)) is True
    metric.assert_any_call("voice.bill.gate_skipped")
    retry = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(retry) == 1 and "Delete" in retry[0]


@pytest.mark.asyncio
async def test_close_call_reraises_when_delete_condition_failed():
    table, client, metronome, patches = _patches()
    client.transact_write_items.side_effect = _cancel("None", "ConditionalCheckFailed")
    with pytest.raises(ClientError):
        await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=90))


@pytest.mark.asyncio
async def test_close_call_reclaim_bills_stored_usage_seconds():
    table, client, metronome, patches = _patches(call={**CALL, "usageSeconds": 90})
    table.update_item.return_value = {"Attributes": {**CALL, "usageSeconds": 90, "status": "call_closing"}}
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=5000)) is True
    event = metronome.ingest.await_args.args[0][0]
    assert event["properties"]["seconds"] == "90"
    assert ("voice.huddle.usage_missing",) not in [c.args for c in metric.call_args_list]


@pytest.mark.asyncio
async def test_persist_failure_still_bills_known_seconds():
    table, client, metronome, patches = _patches()
    table.update_item.side_effect = [
        {"Attributes": {**CALL, "status": "call_closing"}},  # claim
        ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"),  # persist
    ]
    assert await _run(patches, vc.close_call("call-1", now=1100, usage_seconds=90)) is True
    assert metronome.ingest.await_args.args[0][0]["properties"]["seconds"] == "90"
    client.transact_write_items.assert_called_once()


@pytest.mark.asyncio
async def test_wipe_owner_voice_closes_and_deletes_only_this_owners_calls():
    table = MagicMock()
    mine = {**CALL, "callId": "a"}
    other = {**CALL, "callId": "b", "ownerId": "org_b"}
    with (
        patch.object(vc, "_table", return_value=table),
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=[[mine, other], []])),
    ):
        await vc.wipe_owner_voice("org_a")
    keys = [c.kwargs["Key"]["owner_id"] for c in table.delete_item.call_args_list]
    assert keys == ["__call__#a", "__voice__#org_a"]


@pytest.fixture
def moto_tables(monkeypatch):
    """Real DynamoDB semantics: the mocked-client tests above hid typed-value ({"N": ..}) bugs."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name in ("containers-t", "fast-gate-t"):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        containers = ddb.Table("containers-t")
        monkeypatch.setattr(vc, "_table", lambda: containers)
        monkeypatch.setattr(vc, "_fast_gate_table_name", lambda: "fast-gate-t")
        monkeypatch.setattr(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2")
        yield containers, ddb.Table("fast-gate-t")


@pytest.mark.asyncio
async def test_bill_free_against_real_dynamodb(moto_tables):
    containers, _ = moto_tables
    call = {**CALL, "billing": "free", "holdMicrocents": 0, "status": "call_closing"}
    containers.put_item(Item={**call, "owner_id": "__call__#c1"})
    await vc._bill({**call, "callId": "c1"}, 45)
    assert int(containers.get_item(Key={"owner_id": "__voice__#org_a"})["Item"]["usedSeconds"]) == 45
    assert "Item" not in containers.get_item(Key={"owner_id": "__call__#c1"})


@pytest.mark.asyncio
async def test_bill_credits_against_real_dynamodb(moto_tables):
    containers, gate = moto_tables
    call = {**CALL, "callId": "c1", "status": "call_closing"}
    containers.put_item(Item={**call, "owner_id": "__call__#c1"})
    gate.put_item(
        Item={"owner_id": "org_a", "remaining_microcents": 1_000_000, "openrouter_reserved_microcents": 36_000}
    )
    with patch.object(vc, "_metronome", return_value=MagicMock(ingest=AsyncMock())):
        await vc._bill(call, 45)
    row = gate.get_item(Key={"owner_id": "org_a"})["Item"]
    assert int(row["remaining_microcents"]) == 1_000_000 - vc.voice_billing.charge_microcents(45)
    assert int(row["openrouter_reserved_microcents"]) == 0
    assert "Item" not in containers.get_item(Key={"owner_id": "__call__#c1"})


@pytest.mark.parametrize("mode", ["credits", "free", "plan"])
async def test_owner_epoch_uses_existing_settlement_once_without_live_call_workflow(moto_tables, mode):
    """Receipt settlement and debit commit together, including concurrent/replayed ends."""
    import boto3

    from core.billing import voice_billing

    containers, gate = moto_tables
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    messaging = ddb.create_table(
        TableName="owner-usage-t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    gate.put_item(Item={"owner_id": "org_a", "remaining_microcents": 1_000_000})
    account = (
        {"metronome_voice_contract_id": "voice-contract"}
        if mode == "plan"
        else {"provider_choice": "openrouter_managed", "plan": "standard"}
        if mode == "credits"
        else {}
    )
    ledger = MagicMock(ingest=AsyncMock())
    with (
        patch.object(voice_billing.messaging_repo, "_table", return_value=messaging),
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(voice_billing, "report_minutes", AsyncMock()),
        patch.object(vc, "_metronome", return_value=ledger),
    ):
        await voice_billing.report_owner_minutes("org_a", "owner:room:job:0", "2026-11-08T16:00:00Z", 45)
        await voice_billing.report_owner_minutes("org_a", "owner:room:job:0", "2026-11-08T16:10:00Z", 999)
        # A concurrent closer that read the receipt before settlement also cannot debit twice.
        with pytest.raises(ClientError):
            await vc._bill(
                {
                    "ownerId": "org_a",
                    "callId": "owner:room:job:0",
                    "startedAt": 1794153600,
                    "billing": mode,
                    "holdMicrocents": 0,
                    "settlementReceipt": {"pk": "OWNER#org_a", "sk": "VOICEUSAGE#owner:room:job:0"},
                },
                45,
            )
    row = messaging.scan()["Items"][0]
    assert row["billingSettled"] is True and row["seconds"] == 45 and row["billing"] == mode
    assert containers.scan()["Count"] == (1 if mode == "free" else 0)  # no synthetic live call
    balance = int(gate.get_item(Key={"owner_id": "org_a"})["Item"]["remaining_microcents"])
    assert balance == 1_000_000 - (voice_billing.charge_microcents(45) if mode == "credits" else 0)
    if mode == "free":
        assert containers.get_item(Key={"owner_id": "__voice__#org_a"})["Item"]["usedSeconds"] == 45
    if mode == "credits":
        assert ledger.ingest.await_count == 2  # same transaction id, provider deduplicates the concurrent ingest
        assert {c.args[0][0]["transaction_id"] for c in ledger.ingest.await_args_list} == {"owner:room:job:0"}
        assert ledger.ingest.await_args.args[0][0]["event_type"] == "voice_call"
    else:
        ledger.ingest.assert_not_awaited()
