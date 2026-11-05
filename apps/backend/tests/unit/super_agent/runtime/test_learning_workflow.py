"""`LearningWorkflow` on the time-skipping server: FIFO per specialist, parallel across specialists,
retry, curate-after-merges, Continue-As-New; plus the real Activities over moto for review focus 1
(two tasks of one specialist finishing together lose nothing)."""

import asyncio
import hashlib
import json
import time
import uuid
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Worker

from core import dynamodb, tasks_schema as ts
from core.repositories import learning_repo, tasks_repo
from core.services import learning_merge as lm, runtime_runs
from core.temporal import client as temporal_client, learning
from core.temporal.learning import LearningState, LearningWorkflow, MergeRef

WS, SPEC = "org_l", "a1"
T1, T2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


class Fakes:
    def __init__(self, *, merge_delay: float = 0.0, fail_first: bool = False, applied: bool = True):
        self.merges: list[tuple[str, int]] = []
        self.curates = 0
        self.inflight = 0
        self.max_inflight = 0
        self.delay, self.fail_first, self.applied = merge_delay, fail_first, applied
        self.failed = 0

    def activities(self):
        f = self

        @activity.defn(name="merge_learning")
        async def merge_learning(ws: str, specialist_id: str, task_id: str, attempt: int) -> dict:
            f.inflight += 1
            f.max_inflight = max(f.max_inflight, f.inflight)
            try:
                if f.fail_first and not f.failed:
                    f.failed += 1
                    raise RuntimeError("worker died after writing planned")
                if f.delay:
                    await asyncio.sleep(f.delay)
                f.merges.append((task_id, attempt))
                return {"applied": f.applied, "added": {"memory": 1, "user": 0, "skills": []}}
            finally:
                f.inflight -= 1

        @activity.defn(name="curate_specialist")
        async def curate_specialist(ws: str, specialist_id: str) -> dict:
            f.curates += 1
            return {"ran": True, "changed": True}

        return [merge_learning, curate_specialist]


async def _worker(env, activities):
    queue = f"q-{uuid.uuid4()}"
    return queue, Worker(env.client, task_queue=queue, workflows=[LearningWorkflow], activities=activities)


async def _start(env, queue, spec=SPEC, ref: MergeRef | None = None):
    kw = {"start_signal": "merge", "start_signal_args": [ref]} if ref else {}
    return await env.client.start_workflow(
        LearningWorkflow.run, LearningState(WS, spec), id=ts.learning_workflow_id(WS, spec), task_queue=queue, **kw
    )


async def _until(pred, n=400):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_merges_for_one_specialist_run_one_at_a_time_fifo(temporal_env):
    fakes = Fakes(merge_delay=0.1)
    queue, worker = await _worker(temporal_env, fakes.activities())
    async with worker:
        h = await _start(temporal_env, queue, ref=MergeRef(T1, 1))
        await h.signal(LearningWorkflow.merge, MergeRef(T2, 1))
        await h.signal(LearningWorkflow.merge, MergeRef(T1, 2))
        await h.signal(LearningWorkflow.merge, MergeRef(T1, 2))  # a replayed close_task_attempt
        await _until(lambda: len(fakes.merges) == 3)
        assert fakes.merges == [(T1, 1), (T2, 1), (T1, 2)]
        assert fakes.max_inflight == 1
        assert await h.query(LearningWorkflow.pending) == []
        await h.cancel()


async def test_two_specialists_merge_in_parallel(temporal_env):
    fakes = Fakes(merge_delay=0.4)
    queue, worker = await _worker(temporal_env, fakes.activities())
    async with worker:
        a = await _start(temporal_env, queue, "a1", MergeRef(T1, 1))
        b = await _start(temporal_env, queue, "b2", MergeRef(T2, 1))
        await _until(lambda: len(fakes.merges) == 2)
        assert fakes.max_inflight == 2
        await a.cancel()
        await b.cancel()


async def test_worker_crash_mid_activity_retries_and_completes(temporal_env):
    fakes = Fakes(fail_first=True)
    queue, worker = await _worker(temporal_env, fakes.activities())
    async with worker:
        h = await _start(temporal_env, queue, ref=MergeRef(T1, 1))
        await _until(lambda: fakes.merges == [(T1, 1)])
        assert fakes.failed == 1
        await _until(lambda: fakes.curates == 1)
        await h.cancel()


async def test_curate_runs_after_merges_when_due(temporal_env):
    fakes = Fakes()
    queue, worker = await _worker(temporal_env, fakes.activities())
    async with worker:
        h = await _start(temporal_env, queue, ref=MergeRef(T1, 1))
        await h.signal(LearningWorkflow.merge, MergeRef(T2, 1))
        await _until(lambda: fakes.curates == 1)
        assert len(fakes.merges) == 2  # one curate after the queue drained, not one per merge
        await h.cancel()
    skipped = Fakes(applied=False)
    queue, worker = await _worker(temporal_env, skipped.activities())
    async with worker:
        h = await _start(temporal_env, queue, "c3", MergeRef(T1, 1))
        await _until(lambda: len(skipped.merges) == 1)
        await asyncio.sleep(0.3)
        assert skipped.curates == 0  # nothing applied (unsealed / private): no curator run
        await h.cancel()


async def test_continue_as_new_keeps_queue(temporal_env):
    fakes = Fakes()
    queue, worker = await _worker(temporal_env, fakes.activities())
    n = learning.CAN_AFTER_MERGES + 1
    async with worker:
        h = await _start(temporal_env, queue, ref=MergeRef(T1, 1))
        for i in range(2, n + 1):
            await h.signal(LearningWorkflow.merge, MergeRef(T1, i))
        await _until(lambda: len(fakes.merges) == n, n=2000)
        assert [a for _, a in fakes.merges] == list(range(1, n + 1))  # none lost, in order
        # A signal-with-start handle names no run: find the CAN from the CURRENT run's own start
        # event, which points back at the run it continued.
        current = temporal_env.client.get_workflow_handle(ts.learning_workflow_id(WS, SPEC))
        for _ in range(100):
            started = (await current.fetch_history()).events[0].workflow_execution_started_event_attributes
            if started.continued_execution_run_id:
                break
            await asyncio.sleep(0.05)
        assert started.continued_execution_run_id, "the first run never continued as new"
        previous = temporal_client_handle(temporal_env, started.continued_execution_run_id)
        assert (await previous.describe()).status == WorkflowExecutionStatus.CONTINUED_AS_NEW
        assert await current.query(LearningWorkflow.pending) == []
        await current.cancel()


def temporal_client_handle(env, run_id):
    return env.client.get_workflow_handle(ts.learning_workflow_id(WS, SPEC), run_id=run_id)


# --- the real Activities over moto (review focus 1) --------------------------------------------------

BUCKET = "test-owner-state"
SPEC_PREFIX = f"{WS}/hermes/{SPEC}/"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def world(monkeypatch):
    from core.config import settings

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
        resource.create_table(
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
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        puts: list[str] = []
        real_put = s3.put_object

        def counting_put(**kw):
            puts.append(kw["Key"])
            return real_put(**kw)

        s3.put_object = counting_put
        monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
        monkeypatch.setattr(learning, "_s3_client", lambda: s3)
        monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "shared"}))
        monkeypatch.setattr(runtime_runs, "curate", AsyncMock(return_value={"ran": True, "changed": False}))
        yield {"s3": s3, "puts": puts}


def _seed_specialist(s3, memory: str):
    s3.put_object(Bucket=BUCKET, Key=SPEC_PREFIX + lm.MEMORY, Body=memory.encode())
    s3.put_object(Bucket=BUCKET, Key=SPEC_PREFIX + lm.REVISION, Body=b"r0")


def _seed_overlay(s3, task_id: str, attempt: int, *, base_memory: str, memory: str, skill: str):
    prefix = learning.overlay_prefix(WS, task_id, SPEC)
    base = {"files": {lm.MEMORY: {"sha256": _sha(base_memory.encode()), "etag": '"b"', "text": base_memory}}}
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.base_rel(attempt), Body=json.dumps(base).encode())
    skill_md = f"skills/{skill}/SKILL.md"
    files = {lm.MEMORY: memory.encode(), skill_md: f"# {skill}".encode(), "state.db": b"db"}
    for rel, body in files.items():
        s3.put_object(Bucket=BUCKET, Key=prefix + rel, Body=body)
    seal = {"attempt": attempt, "files": {rel: _sha(body) for rel, body in files.items() if rel != "state.db"}}
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.SEALED, Body=json.dumps(seal).encode())


async def test_two_tasks_same_specialist_serialized_no_lost_entries(temporal_env, world, monkeypatch):
    from core.config import settings

    base = "fact one"
    _seed_specialist(world["s3"], base)
    _seed_overlay(world["s3"], T1, 1, base_memory=base, memory=base + lm.DELIM + "learned in task 1", skill="alpha")
    _seed_overlay(world["s3"], T2, 1, base_memory=base, memory=base + lm.DELIM + "learned in task 2", skill="beta")
    queue = f"q-{uuid.uuid4()}"
    monkeypatch.setattr(settings, "TEMPORAL_TASK_QUEUE", queue)
    temporal_client.set_client(temporal_env.client)
    world["puts"].clear()
    async with Worker(
        temporal_env.client, task_queue=queue, workflows=learning.WORKFLOWS, activities=learning.ACTIVITIES
    ):
        await asyncio.gather(  # both attempt ends land together
            learning.signal_merge(WS, SPEC, MergeRef(T1, 1)),
            learning.signal_merge(WS, SPEC, MergeRef(T2, 1)),
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            rows = [await learning_repo.get(WS, SPEC, t, 1) for t in (T1, T2)]
            if all(r and r.get("state") == "applied" for r in rows):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("merges did not apply")
        h = temporal_env.client.get_workflow_handle(ts.learning_workflow_id(WS, SPEC))
        await h.cancel()
    memory = world["s3"].get_object(Bucket=BUCKET, Key=SPEC_PREFIX + lm.MEMORY)["Body"].read().decode()
    entries = memory.split(lm.DELIM)
    assert entries[0] == "fact one" and sorted(entries[1:]) == [
        "learned in task 1",
        "learned in task 2",
    ]  # both, C first
    keys = {o["Key"] for o in world["s3"].list_objects_v2(Bucket=BUCKET, Prefix=SPEC_PREFIX)["Contents"]}
    assert SPEC_PREFIX + "skills/alpha/SKILL.md" in keys and SPEC_PREFIX + "skills/beta/SKILL.md" in keys
    assert world["puts"].count(SPEC_PREFIX + lm.REVISION) == 2  # once per merge, last
    assert not [k for k in world["puts"] if "hermes-tasks" in k]  # the overlays are never written
    for t in (T1, T2):
        assert (await learning_repo.get(WS, SPEC, t, 1))["report"]["applied"] is True
    runtime_runs.curate.assert_awaited_with(WS, SPEC)
