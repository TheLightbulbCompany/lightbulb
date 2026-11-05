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
    monkeypatch.setattr(learning, "tag_overlay", lambda ws, task_id, on: order.append(f"tag:{task_id}") or 0)
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"status": "resolved"}))
    await learning.close_task_attempt(WS, TASK, SPEC, 2, "run-2")
    await learning.close_task_attempt(WS, TASK, SPEC, 2, "run-2")  # a retried end_attempt: same ref again
    assert order == [f"stop:{TASK}:run-2", "signal:LearningWorkflow", f"tag:{TASK}"] * 2
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


@pytest.mark.parametrize("status,tagged", [("waiting", False), ("idle", False), ("resolved", True), ("failed", True)])
@pytest.mark.asyncio
async def test_close_tags_the_overlay_only_when_the_task_is_terminal(world, monkeypatch, status, tagged):
    """#4792 review: S3 Expiration counts from object creation, so a waiting task's >14-day-old overlay
    would expire under the tag. Only a terminal task is tagged; other tasks' overlays and the specialist
    profile never are; a tagging failure never costs the merge signal."""
    s3 = world["s3"]
    _seed(world, lane="shared")
    other = f"{WS}/hermes-tasks/44444444-4444-4444-8444-444444444444/{SPEC}/memories/MEMORY.md"
    s3.put_object(Bucket=BUCKET, Key=other, Body=b"x")
    s3.put_object(Bucket=BUCKET, Key=f"{WS}/hermes/{SPEC}/memories/MEMORY.md", Body=b"profile")
    signalled = []
    monkeypatch.setattr(runtime_runs, "stop_task_session", AsyncMock(return_value=True))
    monkeypatch.setattr(learning, "signal_merge", AsyncMock(side_effect=lambda *a: signalled.append(a)))
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"status": status}))

    await learning.close_task_attempt(WS, TASK, SPEC, 1)

    prefix = learning.overlay_prefix(WS, TASK, SPEC)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)["Contents"]]
    assert len(keys) == 4 and all(_tags(s3, k) == ({"lb-kind": "task-overlay"} if tagged else {}) for k in keys)
    assert _tags(s3, other) == {} and _tags(s3, f"{WS}/hermes/{SPEC}/memories/MEMORY.md") == {}
    assert len(signalled) == 1

    monkeypatch.setattr(learning, "tag_overlay", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3")))
    await learning.close_task_attempt(WS, TASK, SPEC, 2)  # logged, not raised
    assert len(signalled) == 2


def _tags(s3, key):
    return {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}


@pytest.mark.asyncio
async def test_resolve_tags_and_reopen_untags_before_the_next_attempt(world, monkeypatch):
    """Status moves (activities._move, the one choke point): → resolved/failed tags; resolved/failed →
    queued/working (Reopen, Retry) untags BEFORE the transition, so the next attempt never runs on an
    overlay the lifecycle rule is counting down; → waiting tags nothing."""
    from core.temporal import activities

    s3 = world["s3"]
    _seed(world, lane="shared")
    key = learning.overlay_prefix(WS, TASK, SPEC) + lm.MEMORY
    row = {"status": "working"}
    events: list[str] = []

    async def transition(ws, task_id, *, to, reason=None, fields=None):
        events.append(f"{to}:{_tags(s3, key).get('lb-kind', 'none')}")
        row["status"] = to
        return dict(row)

    monkeypatch.setattr(activities, "_task", AsyncMock(side_effect=lambda *a: dict(row)))
    monkeypatch.setattr(activities.tasks_repo, "transition_task", transition)
    monkeypatch.setattr(activities.memory_tasks_index, "index_task", AsyncMock())

    await activities._move(WS, TASK, "waiting")
    assert _tags(s3, key) == {}  # never tagged while waiting
    await activities._move(WS, TASK, "working")
    await activities._move(WS, TASK, "resolved", "resolved")
    assert _tags(s3, key) == {"lb-kind": "task-overlay"}
    await activities._move(WS, TASK, "working")  # Reopen: walks resolved → queued → working
    assert events[-2:] == ["queued:task-open", "working:task-open"]  # untagged before the next attempt


@pytest.mark.asyncio
async def test_reopen_after_the_overlay_expired_merges_against_the_new_attempts_base(world, monkeypatch):
    """Reopened > 14 days after resolve: attempts 1's base/seal/overlay are gone (expired); attempt 2
    restored the specialist onto an empty overlay (the first-attempt path) and wrote base-0002. Its merge
    uses that base -- not "add only" against a missing base-0001 -- and lands the new learning."""
    s3 = world["s3"]
    prefix = learning.overlay_prefix(WS, TASK, SPEC)
    profile = f"{WS}/hermes/{SPEC}/"
    s3.put_object(Bucket=BUCKET, Key=profile + lm.MEMORY, Body=b"old fact")
    old = hashlib.sha256(b"old fact").hexdigest()
    new = b"old fact\n\u00a7\nnew fact"
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.MEMORY, Body=new)
    base2 = {"files": {lm.MEMORY: {"sha256": old, "etag": "", "text": "old fact"}}}
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.base_rel(2), Body=json.dumps(base2).encode())
    seal = {"attempt": 2, "files": {lm.MEMORY: hashlib.sha256(new).hexdigest()}}
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.SEALED, Body=json.dumps(seal).encode())
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "shared", "status": "working"}))
    monkeypatch.setattr(learning.hermes_profiles, "profile_prefix", lambda ws, a: profile)
    gets = []
    real_get = learning._get
    monkeypatch.setattr(learning, "_get", lambda s, b, k: (gets.append(k.rsplit("/", 1)[-1]), real_get(s, b, k))[1])

    report = await learning.merge_learning(WS, SPEC, TASK, 2)

    assert "base-0001.json" in gets and "base-0002.json" in gets  # tried the expired base, fell back
    assert report.get("applied") and report["added"]["memory"] == 1
    assert b"new fact" in s3.get_object(Bucket=BUCKET, Key=profile + lm.MEMORY)["Body"].read()


@pytest.mark.asyncio
async def test_a_failed_restore_never_drops_an_unsealed_attempts_learning(world, monkeypatch):
    """#4792 re-review: attempt 1's restore failed (no base-0001: the bridge writes it only after a
    successful restore); attempt 2 wrote X and died unsealed; attempt 3 restored X from the overlay, so
    base-0003 already holds X, and added Y. The merge takes the FIRST base that exists (base-0002), so X
    and Y both land -- never base-0003, against which X would never be added."""
    s3 = world["s3"]
    prefix = learning.overlay_prefix(WS, TASK, SPEC)
    profile = f"{WS}/hermes/{SPEC}/"
    s3.put_object(Bucket=BUCKET, Key=profile + lm.MEMORY, Body=b"old fact")

    def base(text):
        sha = hashlib.sha256(text.encode()).hexdigest()
        return json.dumps({"files": {lm.MEMORY: {"sha256": sha, "etag": "", "text": text}}}).encode()

    s3.put_object(Bucket=BUCKET, Key=prefix + lm.base_rel(2), Body=base("old fact"))
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.base_rel(3), Body=base("old fact\n§\nfact X"))
    final = b"old fact\n\xc2\xa7\nfact X\n\xc2\xa7\nfact Y"
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.MEMORY, Body=final)
    seal = {"attempt": 3, "files": {lm.MEMORY: hashlib.sha256(final).hexdigest()}}
    s3.put_object(Bucket=BUCKET, Key=prefix + lm.SEALED, Body=json.dumps(seal).encode())
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "shared", "status": "working"}))
    monkeypatch.setattr(learning.hermes_profiles, "profile_prefix", lambda ws, a: profile)

    report = await learning.merge_learning(WS, SPEC, TASK, 3)

    merged = s3.get_object(Bucket=BUCKET, Key=profile + lm.MEMORY)["Body"].read()
    assert report["added"]["memory"] == 2 and b"fact X" in merged and b"fact Y" in merged


@pytest.mark.asyncio
async def test_tag_overlay_skips_an_object_deleted_since_the_listing(world, monkeypatch):
    s3 = world["s3"]
    _seed(world, lane="shared")
    real = s3.put_object_tagging
    gone = learning.overlay_prefix(WS, TASK, SPEC) + lm.MEMORY

    def tagging(**kw):
        if kw["Key"] == gone:
            s3.delete_object(Bucket=BUCKET, Key=gone)
        return real(**kw)

    s3.put_object_tagging = tagging
    assert learning.tag_overlay(WS, TASK) == 3  # the other three still tagged
