"""`LEARN#` intent rows (spec 07 §3.5 crash safety 2-3) on a moto tasks table."""

from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts
from core.repositories import learning_repo
from core.temporal import learning

WS, SPEC, TASK = "org_r", "a1", "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def table(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield t


@pytest.mark.asyncio
async def test_put_planned_idempotent(table):
    planned = {"memories/MEMORY.md": "a" * 64}
    row, created = await learning_repo.put_planned(WS, SPEC, TASK, 2, planned)
    assert created and row["sk"] == ts.learn_sk(SPEC, TASK, 2) == f"LEARN#{SPEC}#{TASK}#0002"
    assert row["state"] == "planned" and row["planned"] == planned and row["type"] == "learn"
    again, created = await learning_repo.put_planned(WS, SPEC, TASK, 2, planned)
    assert not created and again["planned"] == planned and again["version"] == 1
    # A replan (a 412 retry) replaces the shas and bumps the version; the row stays `planned`.
    replan = {**planned, "skills/.usage.json": "b" * 64}
    row, created = await learning_repo.put_planned(WS, SPEC, TASK, 2, replan)
    assert not created and row["planned"] == replan and row["version"] == 2 and row["state"] == "planned"
    assert (await learning_repo.get(WS, SPEC, TASK, 2))["planned"] == replan
    assert await learning_repo.get(WS, SPEC, TASK, 1) is None


@pytest.mark.asyncio
async def test_applied_row_replay_is_noop(table, monkeypatch):
    await learning_repo.put_planned(WS, SPEC, TASK, 1, {})
    report = {"applied": True, "added": {"memory": 1, "user": 0, "skills": []}, "skipped": None}
    await learning_repo.mark_applied(WS, SPEC, TASK, 1, report)
    row = await learning_repo.get(WS, SPEC, TASK, 1)
    assert row["state"] == "applied" and row["report"] == report
    # put_planned never reopens an applied row …
    same, created = await learning_repo.put_planned(WS, SPEC, TASK, 1, {"x": "y"})
    assert not created and same["state"] == "applied" and same["planned"] == {}
    # … and a replayed merge_learning returns the stored report without touching S3 or the task.
    monkeypatch.setattr(learning, "_s3_client", lambda: (_ for _ in ()).throw(AssertionError("S3 touched")))
    monkeypatch.setattr("core.repositories.tasks_repo.get_task", AsyncMock(side_effect=AssertionError("task read")))
    assert await learning.merge_learning(WS, SPEC, TASK, 1) == report
