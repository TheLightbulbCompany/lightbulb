"""`close_task_attempt`: stop the task session, THEN signal the specialist's merge; the end_attempt
hook fires only for task-session attempts; a private-lane attempt merges nothing."""

import hashlib
import json
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts
from core.repositories import learning_repo, tasks_repo
from core.services import learning_merge as lm, runtime_runs, runtime_transport
from core.temporal import learning
from core.temporal.learning import MergeRef

WS, SPEC, TASK = "org_c", "a1", "33333333-3333-4333-8333-333333333333"


@pytest.mark.asyncio
async def test_stops_session_before_signal(monkeypatch):
    order: list[str] = []

    async def stop(ws, task_id, run_id=None):
        order.append(f"stop:{task_id}:{run_id}")
        return True

    class _Client:
        async def start_workflow(self, name, state, **kw):
            order.append(f"signal:{name}")
            self.kw, self.state = kw, state

    client = _Client()
    monkeypatch.setattr(runtime_runs, "stop_task_session", stop)
    monkeypatch.setattr(learning, "get_client", lambda: client)
    await learning.close_task_attempt(WS, TASK, SPEC, 2, "run-2")
    await learning.close_task_attempt(WS, TASK, SPEC, 2, "run-2")  # a retried end_attempt: same ref again
    assert order == [f"stop:{TASK}:run-2", "signal:LearningWorkflow"] * 2
    assert client.kw["id"] == ts.learning_workflow_id(WS, SPEC) == f"learn:{WS}:{SPEC}"
    assert client.kw["start_signal"] == "merge" and client.kw["start_signal_args"] == [MergeRef(TASK, 2)]
    assert client.state.ws == WS and client.state.specialist_id == SPEC


@pytest.mark.asyncio
async def test_end_attempt_hook_only_for_task_sessions(monkeypatch):
    closed: list[tuple] = []

    async def close(ws, task_id, specialist_id, attempt, run_id=None):
        closed.append((ws, task_id, specialist_id, attempt, run_id))

    monkeypatch.setattr(learning, "close_task_attempt", close)
    shared = {"agentId": SPEC, "sessionId": runtime_transport.session_id_for(WS)}
    await learning.close_task_attempt_if_task_session(WS, TASK, shared, 1)
    assert closed == []  # today's path: the shared VM, nothing to stop or merge
    task = {"agentId": SPEC, "runId": "run-3", "sessionId": runtime_transport.session_id_for(WS, TASK)}
    await learning.close_task_attempt_if_task_session(WS, TASK, task, 3)
    assert closed == [(WS, TASK, SPEC, 3, "run-3")]


@pytest.mark.asyncio
async def test_stop_task_session_releases_browser(monkeypatch):
    """§14.11: a task VM stopped while its run held the workspace browser frees it at once; a failed
    release is logged and still answers the stop (the merge signal must follow)."""
    from core.repositories import container_repo
    from core.services import browser_control, hermes_provisioning

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": "arn"}))
    monkeypatch.setattr(hermes_provisioning, "stop_runtime_session", AsyncMock(return_value=True))
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(browser_control, "release_for_run", release)
    assert await runtime_runs.stop_task_session(WS, TASK, "run-3") is True
    release.assert_awaited_once_with(WS, "run-3")
    assert await runtime_runs.stop_task_session(WS, TASK) is True  # no run: no release
    release.assert_awaited_once()
    release.side_effect = RuntimeError("ddb")
    assert await runtime_runs.stop_task_session(WS, TASK, "run-3") is True


BUCKET = "test-owner-state"


@pytest.fixture
def world(monkeypatch):
    from core.config import settings

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        puts: list[str] = []
        real_put = s3.put_object
        s3.put_object = lambda **kw: (puts.append(kw["Key"]), real_put(**kw))[1]
        monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
        monkeypatch.setattr(learning, "_s3_client", lambda: s3)
        yield {"s3": s3, "puts": puts}


def _seed(world, *, lane: str):
    s3 = world["s3"]
    prefix = learning.overlay_prefix(WS, TASK, SPEC)
    memory = b"private fact"
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.MEMORY, Body=memory)
    s3.put_object(Bucket=BUCKET, Key=prefix + "skills/secret/SKILL.md", Body=b"# secret")
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.base_rel(1), Body=json.dumps({"files": {}}).encode())
    seal = {
        "attempt": 1,
        "files": {
            lm.MEMORY: hashlib.sha256(memory).hexdigest(),
            "skills/secret/SKILL.md": hashlib.sha256(b"# secret").hexdigest(),
        },
    }
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.SEALED, Body=json.dumps(seal).encode())
    world["puts"].clear()


@pytest.mark.asyncio
async def test_private_lane_signals_and_merges_nothing(world, monkeypatch):
    _seed(world, lane="private")
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "private"}))
    report = await learning.merge_learning(WS, SPEC, TASK, 1)
    assert report["skipped"] == "private"
    assert report["added"] == {"memory": 1, "user": 0, "skills": ["secret"]}  # listed for Create, not merged
    assert world["puts"] == []  # no S3 put at all
    assert (await learning_repo.get(WS, SPEC, TASK, 1))["state"] == "applied"


@pytest.mark.asyncio
async def test_unsealed_attempt_is_skipped_and_recorded(world, monkeypatch):
    _seed(world, lane="shared")
    world["s3"].delete_object(Bucket=BUCKET, Key=learning.overlay_prefix(WS, TASK, SPEC) + lm.SEALED)
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "shared"}))
    report = await learning.merge_learning(WS, SPEC, TASK, 1)
    assert report["skipped"] == "unsealed" and world["puts"] == []
    assert (await learning_repo.get(WS, SPEC, TASK, 1))["state"] == "applied"
