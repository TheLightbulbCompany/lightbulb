"""`watchdog_act` (wave2-W Task 4): mode gate, consistent re-read, fleet cap, counters spent before the call,
escalate once. Review focus 1 (Paperclip's create-then-cancel loop) and 5 (never touch person-stopped,
resolved or hidden-and-live tasks)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.repositories import watchdog_repo
from core.services import watchdog as wd
from core.temporal.watchdog_types import Proposal, WatchdogConfig

from .conftest import WS

CFG = WatchdogConfig()


def _gone():
    return RPCError("gone", RPCStatusCode.NOT_FOUND, b"")


def _running():
    return SimpleNamespace(
        status=WorkflowExecutionStatus.RUNNING, raw_description=SimpleNamespace(pending_activities=[])
    )


@pytest.fixture
def env(table, metrics):
    """An orphaned working task: row says working, workflow not found. `start` is the reconcile start."""
    tid = ts.new_task_id()
    table.put_item(
        Item=ts.Task(
            ws=WS,
            taskId=tid,
            identifier="LB-1",
            title="t",
            status="working",
            audience={"kind": "workspace"},
            lane="shared",
            triggerId=ts.trigger_id("chat", tid),
            attemptCount=1,
            createdAt="2026-11-04T00:00:00+00:00",
            updatedAt="2026-11-04T00:00:00+00:00",
        ).to_item()
    )
    state = SimpleNamespace(describe=_gone())

    class Handle:
        async def describe(self):
            if isinstance(state.describe, Exception):
                raise state.describe
            return state.describe

    start = AsyncMock()
    with (
        patch("core.temporal.client.task_handle", lambda ws, task_id: Handle()),
        patch("core.temporal.client.start_task_workflow", start),
    ):
        yield SimpleNamespace(table=table, task_id=tid, start=start, state=state, metrics=metrics)


def _p(env, action="reconcile", check="C1"):
    return Proposal(WS, env.task_id, 1, check, action, f"20261104#{WS}#{env.task_id}#{check}#1")


def _set(env, **fields):
    for k, v in fields.items():
        env.table.update_item(
            Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(env.task_id)},
            UpdateExpression="SET #a = :v",
            ExpressionAttributeNames={"#a": k},
            ExpressionAttributeValues={":v": v},
        )


def _wd_rows(env):
    return env.table.query(
        KeyConditionExpression="pk = :p AND begins_with(sk, :w)",
        ExpressionAttributeValues={":p": ts.ws_pk(WS), ":w": f"WD#{env.task_id}#"},
    )["Items"]


async def test_reconcile_act_spends_then_starts(env):
    out = await wd.watchdog_act(CFG, _p(env))
    assert out.outcome == "done"
    env.start.assert_awaited_once_with(WS, env.task_id, "reconcile")
    (row,) = _wd_rows(env)
    assert (row["kind"], row["n"], row["outcome"]) == ("reconcile", 1, "done")
    assert ("watchdog.action", 1.0, {"Kind": "reconcile", "Outcome": "done"}) in env.metrics


async def test_refused_action_spends_the_attempt(env):
    env.start.side_effect = RuntimeError("refused")
    assert (await wd.watchdog_act(CFG, _p(env))).outcome == "failed"
    (row,) = _wd_rows(env)
    assert (row["outcome"], row["reason"]) == ("failed", "RuntimeError")
    assert await watchdog_repo.count_actions(WS, env.task_id, "reconcile") == 1


async def test_paperclip_create_then_cancel_loop_regression(env):
    # Part 1: the harness reproduces Paperclip's bug. Its counter lived on the row it cancelled, and the
    # next scan re-created the row, so a refused admission was retried forever and never escalated.
    rows, escalated = [], False
    for _ in range(100):
        row = {"attempts": 0}  # re-created each scan: the counter resets with it
        row["attempts"] += 1
        if row["attempts"] > 2:
            escalated = True
        rows.append(row)  # admission refused, the row is cancelled, the next tick starts over
    assert (len(rows), escalated) == (100, False)

    # Part 2: the watchdog's counters live on their own rows, written before the call.
    env.start.side_effect = RuntimeError("refused every time")
    outcomes = [(await wd.watchdog_act(CFG, _p(env))).outcome for _ in range(100)]
    assert outcomes[:2] == ["failed", "failed"] and set(outcomes[2:]) == {"limit"}
    assert env.start.await_count == CFG.reconciles_per_task == 2
    kinds = sorted((r["kind"], r["n"]) for r in _wd_rows(env))
    assert kinds == [("escalate", 1), ("reconcile", 1), ("reconcile", 2)]
    findings = await watchdog_repo.query_findings(datetime.now(timezone.utc).strftime("%Y%m%d"), "")
    assert [f["proposed"] for f in findings] == ["escalate"]
    writes = sum(1 for m in env.metrics if m[0] == "watchdog.write")
    # 2 cap units + 2 spends + 2 finishes + escalate (row, finish, finding); ticks at the limit write nothing
    assert writes == 2 + 2 + 2 + 3


async def test_fleet_cap_refusal_writes_no_task_row(env):
    out = await wd.watchdog_act(WatchdogConfig(fleet_actions_per_hour=0), _p(env))
    assert out.outcome == "capped"
    assert _wd_rows(env) == []
    env.start.assert_not_awaited()
    assert ("watchdog.cap_hit", 1.0, {"Kind": "act"}) in env.metrics


@pytest.mark.parametrize(
    "fields",
    [{"stopRequestedBy": "user_x"}, {"status": "resolved"}, {"hidden": True, "live": True}],
)
async def test_reconcile_never_touches_person_stopped_resolved_or_hidden(env, fields):
    if fields.pop("live", False):
        env.state.describe = _running()
    _set(env, **fields)
    assert (await wd.watchdog_act(CFG, _p(env))).outcome == "skipped"
    env.start.assert_not_awaited()
    assert _wd_rows(env) == []


async def test_reconcile_skips_when_describe_errors(env):
    env.state.describe = RPCError("busy", RPCStatusCode.UNAVAILABLE, b"")
    assert (await wd.watchdog_act(CFG, _p(env))).outcome == "skipped"
    env.start.assert_not_awaited()


@pytest.mark.parametrize("action", ["retry", "recheck", "steer", "question"])
@pytest.mark.parametrize("tiers", [("observe", "observe"), ("off", "off"), ("observe", "off")])
async def test_non_reconcile_actions_observe_only(env, action, tiers):
    cfg = WatchdogConfig(tier1=tiers[0], tier2=tiers[1])
    assert (await wd.watchdog_act(cfg, _p(env, action, "C3"))).outcome == "observed"
    env.start.assert_not_awaited()
    assert _wd_rows(env) == []


async def test_reconcile_off_and_observe(env):
    assert (await wd.watchdog_act(WatchdogConfig(reconcile="off"), _p(env))).outcome == "skipped"
    assert (await wd.watchdog_act(WatchdogConfig(reconcile="observe"), _p(env))).outcome == "observed"
    env.start.assert_not_awaited()


async def test_none_action_is_skipped(env):
    assert (await wd.watchdog_act(CFG, _p(env, "none", "judge"))).outcome == "skipped"
