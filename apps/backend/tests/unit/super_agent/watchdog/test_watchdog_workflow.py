"""`WatchdogWorkflow` in the time-skipping env with fake Activities (wave2-W Task 5): tick order, the per-tick
judge cap, the tick metric when off, a failing judge/act never stops the tick; the schedule spec; the worker's
watchdog queue."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from temporalio import activity
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy
from temporalio.worker import Worker

from core.config import settings
from core.services import watchdog as wd
from core.temporal.watchdog_types import ActOut, Candidate, Proposal, ScanOut, WatchdogConfig
from core.temporal.watchdog_workflow import WatchdogWorkflow

OFF = WatchdogConfig(reconcile="off", tier1="off", tier2="off", judge=False)


def _fakes(calls, n_candidates=12, judge_fails=False, act_fails=False):
    @activity.defn(name="watchdog_tick_metric")
    async def tick(mode: str) -> None:
        calls.append(("tick", mode))

    @activity.defn(name="watchdog_scan")
    async def scan(cfg: WatchdogConfig) -> ScanOut:
        calls.append(("scan",))
        cands = [Candidate("ws", f"t{i}", 1, "r", age_min=i) for i in range(n_candidates)]
        return ScanOut(cands, [Proposal("ws", "orphan", 1, "C1", "reconcile", "f-c1")], ["f-c1"])

    @activity.defn(name="watchdog_judge")
    async def judge(cfg: WatchdogConfig, c: Candidate) -> Proposal | None:
        calls.append(("judge", c.age_min))
        if judge_fails:
            raise RuntimeError("jev blew up")
        return Proposal("ws", c.task_id, 1, "judge", "none" if c.age_min % 2 else "steer", f"f-{c.task_id}", 0.9)

    @activity.defn(name="watchdog_act")
    async def act(cfg: WatchdogConfig, p: Proposal) -> ActOut:
        calls.append(("act", p.action))
        if act_fails:
            raise RuntimeError("act blew up")
        return ActOut("done" if p.action == "reconcile" and cfg.reconcile == "act" else "observed")

    @activity.defn(name="watchdog_digest")
    async def digest(cfg: WatchdogConfig, ids: list[str], daily: bool) -> None:
        calls.append(("digest", len(ids)))

    return [tick, scan, judge, act, digest]


async def _tick(env, cfg, calls, **kw):
    queue = f"wd-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=[WatchdogWorkflow], activities=_fakes(calls, **kw)):
        return await env.client.execute_workflow(
            WatchdogWorkflow.run,
            cfg,
            id=f"watchdog-{uuid.uuid4()}",
            task_queue=queue,
            execution_timeout=timedelta(minutes=4),
        )


async def test_watchdog_tick_order_scan_judge_act_digest(temporal_env):
    calls = []
    report = await _tick(temporal_env, WatchdogConfig(reconcile="act", judge_per_tick=3), calls)
    kinds = [c[0] for c in calls]
    assert kinds[:2] == ["tick", "scan"] and kinds[-1] == "digest"
    assert [c[1] for c in calls if c[0] == "judge"] == [11, 10, 9]  # oldest attempts first, capped per tick
    assert kinds.index("act") > max(i for i, k in enumerate(kinds) if k == "judge")
    assert [c[1] for c in calls if c[0] == "act"] == ["reconcile", "steer"]  # "none" is never acted on
    assert (report.findings_new, report.judged, report.acted, report.mode) == (4, 3, 1, "act")


async def test_default_config_observes_reconcile_until_w6(temporal_env):
    calls = []
    report = await _tick(temporal_env, WatchdogConfig(), calls)
    assert report.mode == "observe"
    acts = [c[1] for c in calls if c[0] == "act"]
    assert acts[0] == "reconcile" and report.acted == 0  # act runs, records observed, performs nothing


async def test_tick_metric_emitted_when_off(temporal_env):
    calls = []
    report = await _tick(temporal_env, OFF, calls)
    assert calls == [("tick", "off")]
    assert report.mode == "off"


async def test_failing_judge_and_act_never_stop_the_tick(temporal_env):
    calls = []
    report = await _tick(temporal_env, WatchdogConfig(judge_per_tick=2), calls, judge_fails=True, act_fails=True)
    assert calls[-1] == ("digest", 1)
    assert (report.judged, report.acted) == (0, 0)


# --- schedule ----------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.created = []

    async def create_schedule(self, sid, schedule):
        if self.created:
            raise ScheduleAlreadyRunningError()
        self.created.append((sid, schedule))


async def test_watchdog_schedule_skip_and_4_min_timeout(monkeypatch):
    client = FakeClient()
    assert await wd.ensure_watchdog_schedule(client) is True
    sid, sched = client.created[0]
    assert sid == "watchdog"
    assert sched.spec.intervals[0].every == timedelta(minutes=5)
    assert sched.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert sched.policy.catchup_window == timedelta(minutes=1)
    assert sched.action.workflow == "WatchdogWorkflow"
    assert sched.action.task_queue == "lightbulb-watchdog"
    assert sched.action.execution_timeout == timedelta(minutes=4)
    assert sched.action.args == [WatchdogConfig()]
    assert await wd.ensure_watchdog_schedule(client) is False  # existing (maybe changed) args are kept
    assert len(client.created) == 1


# --- worker queue ------------------------------------------------------------------------------------


@pytest.mark.parametrize("queue", ["lightbulb", "lightbulb-watchdog"])
async def test_worker_watchdog_queue_registers_only_watchdog_lists(monkeypatch, queue):
    from core.temporal import client as tc
    from core.temporal.registry import ALL_ACTIVITIES, ALL_WORKFLOWS
    from core.temporal.watchdog_workflow import WATCHDOG_WORKFLOWS
    from scripts import temporal_worker

    monkeypatch.setattr(tc, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(tc, "set_client", lambda c: None)
    ensure = AsyncMock()
    monkeypatch.setattr(wd, "ensure_watchdog_schedule", ensure)
    seen = {}

    class StopHere(Exception):
        pass

    def fake_worker(client, *, task_queue, workflows, activities, **kw):
        seen.update(queue=task_queue, workflows=workflows, activities=activities)
        raise StopHere

    monkeypatch.setattr("temporalio.worker.Worker", fake_worker)
    with pytest.raises(StopHere):
        await temporal_worker.run(queue)
    assert seen["queue"] == queue
    if queue == "lightbulb-watchdog":
        assert (seen["workflows"], seen["activities"]) == (WATCHDOG_WORKFLOWS, wd.WATCHDOG_ACTIVITIES)
    else:
        assert (seen["workflows"], seen["activities"]) == (ALL_WORKFLOWS, ALL_ACTIVITIES)
    ensure.assert_not_awaited()  # runs inside the worker context, which the fake never enters


async def test_worker_refuses_an_unknown_queue(monkeypatch):
    """`--queue` that matches neither configured queue exits 1 instead of quietly becoming a second main worker."""
    from core.temporal import client as tc
    from scripts import temporal_worker

    monkeypatch.setattr(settings, "TEMPORAL_WATCHDOG_TASK_QUEUE", "lightbulb-watchdog-renamed")
    connect = AsyncMock()
    monkeypatch.setattr(tc, "connect", connect)
    assert await temporal_worker.run("lightbulb-watchdog") == 1
    connect.assert_not_awaited()
    assert await temporal_worker.run("nope") == 1
