"""Reconcile: the SCHED# row is the truth; Temporal is repaired to match (lane A, Task 4)."""

from datetime import datetime, timedelta, timezone

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts
from core.repositories import automations_repo as repo
from core.services import automation_schedule as asch
from core.temporal.automations import reconcile_schedules


@pytest.fixture
def table(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
        t = resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": g.name,
                    "KeySchema": [
                        {"AttributeName": g.pk_attr, "KeyType": "HASH"},
                        {"AttributeName": g.sk_attr, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for g in ts.GSIS
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield t


def _sched(sid, **over):
    return {
        "scheduleId": sid,
        "name": "n",
        "ownerUserId": "user_1",
        "cron": ["0 9 * * 1"],
        "tz": "America/New_York",
        "policyText": "p",
    } | over


async def _active(ws, sid, **over):
    await repo.create_schedule(ws, _sched(sid, **over))
    return await repo.set_status(ws, sid, "active", reason=None, allowed_from=frozenset({"draft"}))


async def _describe(client, ws, sid):
    return await client.get_schedule_handle(ts.schedule_workflow_id(ws, sid)).describe()


async def _cleanup(ws, *sids):
    for sid in sids:
        await asch.delete(ws, sid)


async def test_reconcile_creates_missing(table, client, ws):
    row = await _active(ws, "a")
    try:
        out = await reconcile_schedules()
        assert out["created"] >= 1
        d = await _describe(client, ws, "a")
        assert d.schedule.state.paused is False
        assert asch.fingerprint(row) == asch.fingerprint_of(d)
        again = await reconcile_schedules()
        assert again["created"] == 0
    finally:
        await _cleanup(ws, "a")


async def test_reconcile_fixes_drift_cron_tz_paused(table, client, ws):
    row = await _active(ws, "b")
    await asch.upsert(row | {"cron": ["0 10 * * 2"], "tz": "UTC", "status": "paused"})  # Temporal drifted
    try:
        out = await reconcile_schedules()
        assert out["updated"] >= 1
        d = await _describe(client, ws, "b")
        assert d.schedule.state.paused is False
        assert d.schedule.spec.time_zone_name == "America/New_York"
        assert asch.fingerprint_of(d) == asch.fingerprint(row)
    finally:
        await _cleanup(ws, "b")


async def test_reconcile_deletes_orphan_schedule(table, client, ws):
    await asch.upsert({"pk": f"WS#{ws}", "scheduleId": "gone", "cron": ["0 9 * * 1"], "tz": "UTC", "status": "active"})
    out = await reconcile_schedules()
    assert out["deleted_orphans"] >= 1
    from temporalio.service import RPCError

    with pytest.raises(RPCError):
        await _describe(client, ws, "gone")


async def test_reconcile_keeps_schedule_whose_row_appeared_after_the_scan(table, client, ws, monkeypatch):
    row = await _active(ws, "late")
    await asch.upsert(row)
    monkeypatch.setattr(repo, "scan_schedules", AsyncMock(return_value=[]))  # the scan missed it
    try:
        out = await reconcile_schedules()
        assert out["deleted_orphans"] == 0
        assert (await _describe(client, ws, "late")).schedule.state.paused is False
    finally:
        await _cleanup(ws, "late")


async def test_reconcile_acts_on_the_current_row_not_the_scanned_one(table, client, ws, monkeypatch):
    """A pause (or delete) after the scan must not be undone by upserting the stale scanned row."""
    stale = await _active(ws, "p")
    await asch.upsert(stale)
    await repo.set_status(ws, "p", "paused", reason=None, allowed_from=frozenset({"active"}))
    await asch.pause(ws, "p", "paused by Sam")
    gone = await _active(ws, "g")
    await repo.delete_schedule(ws, "g")
    monkeypatch.setattr(repo, "scan_schedules", AsyncMock(return_value=[stale, gone]))
    try:
        await reconcile_schedules()
        assert (await _describe(client, ws, "p")).schedule.state.paused is True
        from temporalio.service import RPCError

        with pytest.raises(RPCError):  # a row deleted after the scan gets no Schedule
            await _describe(client, ws, "g")
    finally:
        await _cleanup(ws, "p", "g")


async def test_reconcile_refuses_deletes_when_table_is_another_env(table, client, ws, monkeypatch):
    from core.temporal import automations

    await asch.upsert({"pk": f"WS#{ws}", "scheduleId": "orph", "cron": ["0 9 * * 1"], "tz": "UTC", "status": "active"})
    monkeypatch.setattr(automations, "sweep_allowed", lambda: False)
    try:
        out = await reconcile_schedules()
        assert out["deleted_orphans"] == 0
        await _describe(client, ws, "orph")  # still there
    finally:
        await _cleanup(ws, "orph")


async def test_event_only_automation_has_no_schedule(table, client, ws):
    row = await _active(ws, "ev", cron=[])
    assert await asch.upsert(row) is None
    await asch.upsert(row | {"cron": ["0 9 * * 1"]})  # it had one, then became event-only
    assert await asch.upsert(row) == "updated"
    from temporalio.service import RPCError

    with pytest.raises(RPCError):
        await _describe(client, ws, "ev")


async def test_reconcile_deletes_stale_draft(table, client, ws):
    row, _ = await repo.create_schedule(ws, _sched("d", hookPublicIds=["pub_d"]))
    await repo.put_hook(
        ws,
        {
            "publicId": "pub_d",
            "scheduleId": "d",
            "provider": "stripe",
            "signingMode": "hmac_sha256",
            "secretRef": "hook-pub_d",
        },
    )
    await asch.upsert(row)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    table.update_item(
        Key={"pk": row["pk"], "sk": row["sk"]},
        UpdateExpression="SET createdAt = :o",
        ExpressionAttributeValues={":o": old},
    )
    fresh, _ = await repo.create_schedule(ws, _sched("f"))
    try:
        with patch("core.services.credentials.integration_secret_store.delete", AsyncMock(return_value=True)) as rm:
            out = await reconcile_schedules()
        rm.assert_awaited_once_with(ws, "hook-pub_d")
        assert out["deleted_drafts"] >= 1
        assert await repo.get_schedule(ws, "d") is None
        assert await repo.get_hook(ws, "pub_d") is None
        assert await repo.get_schedule(ws, "f") is not None  # a fresh draft stays
        from temporalio.service import RPCError

        with pytest.raises(RPCError):
            await _describe(client, ws, "d")
    finally:
        await _cleanup(ws, "f")


async def test_reconcile_boot_start_idempotent(client):
    import uuid

    from core.temporal.automations import ScheduleReconcileWorkflow

    queue = f"it-{uuid.uuid4()}"
    await asch.ensure_platform(client, task_queue=queue)
    await asch.ensure_platform(client, task_queue=queue)  # second boot: no error, same workflow
    d = await client.get_schedule_handle(asch.PLATFORM_SCHEDULE_ID).describe()
    assert d.schedule.spec.time_zone_name in (None, "", "UTC")
    h = client.get_workflow_handle(asch.RECONCILE_WORKFLOW_ID)
    assert (await h.describe()).workflow_type == ScheduleReconcileWorkflow.__name__
    await h.terminate()
    await client.get_schedule_handle(asch.PLATFORM_SCHEDULE_ID).delete()
