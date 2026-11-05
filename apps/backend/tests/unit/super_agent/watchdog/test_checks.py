"""Code checks C1–C6 and `watchdog_scan` (wave2-W Task 2) against moto, a fake Temporal handle and a fake
runtime. Review focus 2 (GSI lag) and 3 (describe errors other than NOT_FOUND)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.repositories import tasks_repo, watchdog_repo
from core.services import watchdog as wd
from core.temporal.watchdog_types import WatchdogConfig

from .conftest import WS

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CFG = WatchdogConfig(reconcile="act")
OFF = WatchdogConfig(reconcile="off", tier1="off", tier2="off", judge=False)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _task(status="working", age=10, **over):
    return {
        "pk": ts.ws_pk(WS),
        "taskId": over.pop("taskId", ts.new_task_id()),
        "status": status,
        "updatedAt": _iso(age),
        "attemptCount": 1,
        "ownerUserId": "user_sam",
    } | over


def _wf(status="RUNNING", turn=True, hb_age=1):
    return wd.WfState(status, NOW - timedelta(minutes=hb_age) if turn else None, turn)


# --- pure checks -------------------------------------------------------------------------------------


def test_c1_orphan_detected_when_workflow_closed():
    assert wd.check_c1(_task(), _wf("COMPLETED"), NOW)
    assert wd.check_c1(_task("queued"), _wf("TERMINATED"), NOW)
    assert not wd.check_c1(_task(), _wf(), NOW)


def test_c1_orphan_detected_when_not_found():
    assert wd.check_c1(_task(), None, NOW)
    assert not wd.check_c1(_task("failed"), None, NOW)


def test_c1_not_before_5_min():
    assert not wd.check_c1(_task(age=4), None, NOW)
    assert wd.check_c1(_task(age=5), None, NOW)


def test_c2_thresholds():
    assert wd.check_c2(_task(age=31), _wf(hb_age=31), CFG, NOW)
    assert wd.check_c2(_task(age=31), _wf(turn=False), CFG, NOW)
    assert not wd.check_c2(_task(age=31), _wf(hb_age=5), CFG, NOW)
    assert not wd.check_c2(_task(age=10), _wf(turn=False), CFG, NOW)  # the row moved recently
    assert not wd.check_c2(_task(age=31), None, CFG, NOW)


def _failed(error=None, outcome="failed", age=20, **over):
    return _task("failed", age=age, **over), {"outcome": outcome, "error": error}


@pytest.mark.parametrize(
    ("error", "outcome", "ok"),
    [
        (None, "runtime_lost", True),
        ("provider_5xx", "failed", True),
        ("provider_rate_limited", "failed", True),
        ("recycling", "failed", True),
        ("integration_unavailable:google", "failed", True),
        ("RunFailed", "failed", False),
        (None, "failed", False),
    ],
)
def test_c3_retryable_classes_only(error, outcome, ok):
    assert wd.check_c3(*_failed(error, outcome), NOW) is ok


def test_c3_unknown_error_class_never_retryable():
    assert not wd.check_c3(*_failed("provider_teapot"), NOW)
    assert not wd.check_c3(*_failed("something_new"), NOW)


def test_c3_never_after_person_stop():
    assert not wd.check_c3(*_failed(outcome="runtime_lost", stopRequestedBy="user_x"), NOW)


def test_c3_never_hidden():
    assert not wd.check_c3(*_failed(outcome="runtime_lost", hidden=True), NOW)


def test_c3_requires_owner_user():
    assert not wd.check_c3(*_failed(outcome="runtime_lost", ownerUserId=None), NOW)


def test_c3_waits_ten_minutes_with_jitter():
    task, att = _failed(outcome="runtime_lost", age=7.9)
    assert not wd.check_c3(task, att, NOW)
    task, att = _failed(outcome="runtime_lost", age=12.1)
    assert wd.check_c3(task, att, NOW)
    assert abs(wd._jitter(task["taskId"]).total_seconds()) <= 120


def test_c4_backoff_schedule_5_to_160():
    due = [wd.next_recheck_due(n, NOW) for n in range(7)]
    assert [(d - NOW).total_seconds() / 60 for d in due[:6]] == [5, 10, 20, 40, 80, 160]
    assert due[6] is None


def _tools(*calls):
    return [{"name": n, "argsHash": h, "ok": e is None, "errorClass": e} for n, h, e in calls]


def test_c5_repeat_thresholds_4_same_call_3_same_error():
    assert wd.check_c5(_tools(*[("read", "a", None)] * 4))
    assert not wd.check_c5(_tools(*[("read", "a", None)] * 3, ("read", "b", None), ("read", "a", None)))
    assert wd.check_c5(_tools(("gh", "1", "auth"), ("gh", "2", "auth"), ("gh", "3", "auth")))
    assert not wd.check_c5(_tools(("gh", "1", "auth"), ("gh", "2", "auth"), ("gh", "3", None)))
    assert not wd.check_c5(_tools(("a", "1", None), ("b", "2", None), ("c", "3", None)))


def test_c6_gate_age_and_cadence():
    att = {"startedAt": _iso(25)}
    assert wd.check_c6(att, True, None, CFG, NOW)
    assert not wd.check_c6(att, False, None, CFG, NOW)
    att = {"startedAt": _iso(61)}
    assert wd.check_c6(att, False, NOW - timedelta(minutes=31), CFG, NOW)
    assert not wd.check_c6(att, False, NOW - timedelta(minutes=10), CFG, NOW)


# --- scan against moto -------------------------------------------------------------------------------


class FakeHandle:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def describe(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _desc(status="RUNNING", turn=True, hb_age=1):
    acts = []
    if turn:
        hb = SimpleNamespace(ToDatetime=lambda tzinfo: NOW - timedelta(minutes=hb_age))
        acts.append(
            SimpleNamespace(
                activity_type=SimpleNamespace(name="worker_turn"), HasField=lambda f: True, last_heartbeat_time=hb
            )
        )
    return SimpleNamespace(
        status=WorkflowExecutionStatus[status], raw_description=SimpleNamespace(pending_activities=acts)
    )


async def _put_task(table, status, age, **over):
    task_id = over.pop("taskId", ts.new_task_id())
    row = ts.Task(
        ws=WS,
        taskId=task_id,
        identifier="LB-1",
        title="t",
        status=status,
        audience={"kind": "workspace"},
        lane="shared",
        ownerUserId="user_sam",
        triggerId=ts.trigger_id("chat", task_id),
        attemptCount=over.pop("attemptCount", 1),
        createdAt=_iso(age),
        updatedAt=_iso(age),
        **over,
    ).to_item()
    table.put_item(Item=row)
    return task_id


@pytest.fixture
def scan_env(table, metrics, monkeypatch):
    """Workspace WS on; a describe() result per task id; a fixed clock; a fake run status."""
    monkeypatch.setattr(wd, "_scope", (0.0, []))
    handles: dict[str, FakeHandle] = {}
    default = FakeHandle(_desc())

    def handle(ws, task_id):
        return handles.get(task_id, default)

    run = AsyncMock(return_value=SimpleNamespace(recent_tools=[]))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    with (
        patch("core.services.super_agent_flags.list_on", AsyncMock(return_value=[WS])) as list_on,
        patch("core.temporal.client.task_handle", handle),
        patch.object(wd.runtime_runs, "get_run", run),
        patch.object(wd, "datetime", Clock),
    ):
        yield SimpleNamespace(table=table, handles=handles, run=run, metrics=metrics, list_on=list_on)


def _findings(table):
    resp = table.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": ts.wd_finding_pk("20261104")})
    return resp["Items"]


async def test_scan_c1_writes_finding_and_reconcile_proposal(scan_env):
    tid = await _put_task(scan_env.table, "working", 10)
    scan_env.handles[tid] = FakeHandle(RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
    out = await wd.watchdog_scan(CFG)
    assert [(p.check, p.action) for p in out.proposals] == [("C1", "reconcile")]
    (f,) = _findings(scan_env.table)
    assert (f["check"], f["mode"], f["proposed"]) == ("C1", "act", "reconcile")
    assert out.finding_ids == [f["findingId"]]
    again = await wd.watchdog_scan(CFG)
    assert again.finding_ids == [] and len(_findings(scan_env.table)) == 1  # deterministic key


async def test_c1_rereads_row_consistently_before_finding(scan_env, monkeypatch):
    tid = await _put_task(scan_env.table, "working", 10)
    scan_env.handles[tid] = FakeHandle(RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
    real = tasks_repo.get_task

    async def moved_on(ws, task_id):  # the GSI page still says working; the row is idle
        return (await real(ws, task_id)) | {"status": "idle"}

    monkeypatch.setattr(tasks_repo, "get_task", moved_on)
    out = await wd.watchdog_scan(CFG)
    assert out.proposals == [] and _findings(scan_env.table) == []
    assert scan_env.handles[tid].calls == 0


async def test_c1_describe_error_is_not_orphan(scan_env):
    tid = await _put_task(scan_env.table, "working", 10)
    scan_env.handles[tid] = FakeHandle(RPCError("busy", RPCStatusCode.UNAVAILABLE, b""))
    out = await wd.watchdog_scan(CFG)
    assert out.proposals == [] and _findings(scan_env.table) == []
    assert ("watchdog.scan_error", 1.0, {"Check": "C1"}) in scan_env.metrics


async def test_c2_stall_emits_task_stuck_and_records_only(scan_env):
    tid = await _put_task(scan_env.table, "working", 40)
    scan_env.handles[tid] = FakeHandle(_desc(hb_age=31))
    out = await wd.watchdog_scan(CFG)
    (f,) = _findings(scan_env.table)
    assert (f["check"], f["proposed"], f["mode"]) == ("C2", "none", "observe")
    assert out.proposals == []
    assert any(m[0] == "task.stuck" for m in scan_env.metrics)


async def test_observe_writes_finding_only(scan_env):
    tid = await _put_task(scan_env.table, "failed", 20)
    await tasks_repo.put_attempt(
        WS,
        tid,
        1,
        {"agentId": "a", "sessionId": "s", "status": "ended", "outcome": "runtime_lost", "startedAt": _iso(30)},
    )
    out = await wd.watchdog_scan(CFG)
    (f,) = _findings(scan_env.table)
    assert (f["check"], f["proposed"], f["mode"]) == ("C3", "retry", "observe")
    assert [(p.check, p.action) for p in out.proposals] == [("C3", "retry")]
    actions = scan_env.table.query(
        KeyConditionExpression="pk = :p AND begins_with(sk, :w)",
        ExpressionAttributeValues={":p": ts.ws_pk(WS), ":w": "WD#"},
    )["Items"]
    assert actions == []


async def test_c4_external_error_proposes_recheck(scan_env):
    tid = await _put_task(scan_env.table, "failed", 20)
    await tasks_repo.put_attempt(
        WS,
        tid,
        1,
        {
            "agentId": "a",
            "sessionId": "s",
            "status": "ended",
            "outcome": "failed",
            "error": "integration_unavailable:google",
            "startedAt": _iso(30),
        },
    )
    out = await wd.watchdog_scan(CFG)
    assert [(p.check, p.action) for p in out.proposals] == [("C4", "recheck")]


async def test_scan_judge_candidate_from_c5(scan_env):
    tid = await _put_task(scan_env.table, "working", 2)
    await tasks_repo.put_attempt(
        WS, tid, 1, {"agentId": "a", "sessionId": "s", "status": "running", "startedAt": _iso(25)}
    )
    scan_env.run.return_value = SimpleNamespace(recent_tools=_tools(*[("read", "a", None)] * 4))
    out = await wd.watchdog_scan(CFG)
    (c,) = out.candidates
    assert (c.task_id, c.attempt_n, c.reasons, c.age_min) == (tid, 1, ["C5", "C6"], 25)
    assert [f["check"] for f in _findings(scan_env.table)] == ["C5"]


async def test_off_reads_nothing(scan_env, monkeypatch):
    calls = AsyncMock()
    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", calls)
    out = await wd.watchdog_scan(OFF)
    assert out.proposals == out.candidates == []
    calls.assert_not_awaited()
    scan_env.list_on.assert_not_awaited()


async def test_scan_skips_workspaces_not_on(scan_env, monkeypatch):
    scan_env.list_on.return_value = []
    await _put_task(scan_env.table, "working", 10)
    calls = AsyncMock(return_value=([], None))
    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", calls)
    await wd.watchdog_scan(CFG)
    calls.assert_not_awaited()


async def test_hint_person_stopped(scan_env):
    tid = await _put_task(scan_env.table, "working", 40)
    scan_env.handles[tid] = FakeHandle(_desc(hb_age=31))
    await wd.watchdog_scan(CFG)
    scan_env.table.update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(tid)},
        UpdateExpression="SET stopRequestedBy = :u",
        ExpressionAttributeValues={":u": "user_sam"},
    )
    await wd.watchdog_scan(CFG)
    (f,) = await watchdog_repo.query_findings("20261104", "")
    assert f["hint"] == "person_stopped"


async def test_one_workspace_error_does_not_abort_the_tick(scan_env, monkeypatch):
    scan_env.list_on.return_value = ["org_bad", WS]
    tid = await _put_task(scan_env.table, "working", 10)
    scan_env.handles[tid] = FakeHandle(RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
    real = tasks_repo.list_tasks_by_status

    async def flaky(ws, status, **kw):
        if ws == "org_bad":
            raise ValueError("bad row")
        return await real(ws, status, **kw)

    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", flaky)
    out = await wd.watchdog_scan(CFG)
    assert [(p.ws, p.check) for p in out.proposals] == [(WS, "C1")]
    assert ("watchdog.scan_error", 1.0, {"Check": "workspace"}) in scan_env.metrics
