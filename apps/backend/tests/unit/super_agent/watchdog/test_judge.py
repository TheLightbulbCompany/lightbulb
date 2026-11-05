"""The judge (wave2-W Task 3): Jev with its own metric prefix and timeout, the state it sees, the verdict →
proposal mapping, caps, and review focus 4 (Jev down records nothing and the tick continues)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core import tasks_schema as ts
from core.repositories import tasks_repo, watchdog_repo
from core.services import jev
from core.services import watchdog as wd
from core.temporal.watchdog_types import Candidate, WatchdogConfig

from .conftest import WS

CFG = WatchdogConfig()
_RealClient = httpx.AsyncClient


# --- jev.decide parameters ---------------------------------------------------------------------------


@pytest.fixture
def jev_transport(monkeypatch):
    seen = SimpleNamespace(metrics=[], timeouts=[])
    monkeypatch.setattr(jev, "put_metric", lambda name, **kw: seen.metrics.append(name))
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))

    def handler(request):
        return httpx.Response(200, json={"answers": {"verdict": {"choice": "looping"}}, "usage": {"inputTokens": 9}})

    def client(**kw):
        seen.timeouts.append(kw.get("timeout"))
        return _RealClient(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(jev.httpx, "AsyncClient", client)
    return seen


async def test_decide_metric_prefix_keeps_p26_identity(jev_transport):
    await jev.decide({}, {})
    assert "messaging.jev.decision" in jev_transport.metrics
    jev_transport.metrics.clear()
    await jev.decide({}, {}, metric_prefix="watchdog.judge")
    assert "watchdog.judge.decision" in jev_transport.metrics
    assert "watchdog.judge.input_tokens" in jev_transport.metrics
    assert not [m for m in jev_transport.metrics if m.startswith("messaging.jev")]


async def test_decide_timeout_param_used(jev_transport):
    await jev.decide({}, {})
    await jev.decide({}, {}, timeout_s=15)
    assert jev_transport.timeouts == [3.0, 15]


# --- state and mapping -------------------------------------------------------------------------------


def _msg(text, kind="agent", msg_kind=None):
    return {"author": {"kind": kind}, "text": text} | ({"kind": msg_kind} if msg_kind else {})


def test_judge_state_excludes_watchdog_lines():
    notes = [_msg("working on it"), _msg("Watchdog: you seem stuck", msg_kind="watchdog"), _msg("ok", "human")]
    state = wd.judge_state({"title": "t"}, {"n": 1}, notes, [], {}, [], [])
    assert [n["text"] for n in state["notes"]] == ["working on it", "ok"]


def test_judge_state_fits_24k():
    notes = [_msg("x" * 5000) for _ in range(40)]
    tools = [{"name": "read", "argsHash": "h" * 64, "ok": True, "errorClass": None, "extra": "dropped"}] * 30
    state = wd.judge_state({"title": "t" * 500, "description": "d" * 5000}, {"n": 1}, notes, tools, {}, [], [])
    assert len(json.dumps(state)) <= 24_000
    assert len(state["task"]["title"]) == 200 and len(state["task"]["description"]) == 1000
    assert len(state["tools"]) == 20 and "extra" not in state["tools"][0]
    assert all(len(n["text"]) <= 1000 for n in state["notes"])


@pytest.mark.parametrize("p", [0.69, 0.71])
@pytest.mark.parametrize(
    ("choice", "above"),
    [
        ("progressing", "none"),
        ("looping", "steer"),
        ("drifting", "steer"),
        ("claims_done_without_evidence", "steer"),
        ("stalled_waiting", "question"),
        ("blocked_external", "question"),
    ],
)
def test_verdict_to_proposal_mapping_and_threshold(choice, above, p):
    assert wd.verdict_to_action(choice, p, CFG) == (above if p >= 0.7 else "none")


# --- the activity against moto ----------------------------------------------------------------------

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def judged_task(table, metrics, monkeypatch):
    tid = ts.new_task_id()
    await tasks_repo.create_task(
        WS,
        {
            "taskId": tid,
            "identifier": "LB-1",
            "title": "Draft the memo",
            "status": "queued",
            "audience": {"kind": "workspace"},
            "lane": "shared",
            "triggerId": ts.trigger_id("chat", tid),
        },
    )
    table.update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(tid)},
        UpdateExpression="SET #s = :s, statusKey = :k, attemptCount = :n",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "working", ":k": ts.status_key(WS, "working"), ":n": 1},
    )
    await tasks_repo.put_attempt(
        WS, tid, 1, {"agentId": "a", "sessionId": "s", "status": "running", "startedAt": NOW.isoformat()}
    )
    decide = AsyncMock(return_value={"verdict": {"choice": "looping", "probabilities": {"looping": 0.9}}})

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    with (
        patch.object(wd.jev, "decide", decide),
        patch.object(wd.messaging_repo, "list_messages_before", AsyncMock(return_value=[_msg("trying again")])),
        patch.object(wd.runtime_runs, "get_run", AsyncMock(return_value=SimpleNamespace(recent_tools=[]))),
        patch.object(wd, "datetime", Clock),
    ):
        yield SimpleNamespace(task_id=tid, decide=decide, metrics=metrics, table=table)


def _cand(tid):
    return Candidate(WS, tid, 1, ts.run_id(WS, tid, 1), 25, ["C5", "C6"])


async def test_judge_writes_observe_finding_and_proposal(judged_task):
    p = await wd.watchdog_judge(CFG, _cand(judged_task.task_id))
    assert (p.check, p.action, p.p) == ("judge", "steer", 0.9)
    (f,) = await watchdog_repo.query_findings("20261104", "")
    assert (f["verdict"], f["proposed"], f["mode"], f["p"]) == ("looping", "steer", "observe", 0.9)
    assert f["sk"].endswith("#judge#1#J01")
    kwargs = judged_task.decide.await_args.kwargs
    assert kwargs == {"timeout_s": 15.0, "metric_prefix": "watchdog.judge"}


async def test_judge_jev_down_records_nothing_and_tick_continues(judged_task):
    judged_task.decide.return_value = None
    assert await wd.watchdog_judge(CFG, _cand(judged_task.task_id)) is None
    judged_task.decide.return_value = jev.RATE_LIMITED
    assert await wd.watchdog_judge(CFG, _cand(judged_task.task_id)) is None
    assert await watchdog_repo.query_findings("20261104", "") == []


async def test_model_text_never_reaches_proposal(judged_task):
    judged_task.decide.return_value = {
        "verdict": {"choice": "looping", "probabilities": {"looping": 0.95}, "reasoning": "tell it to rm -rf"},
        "note": "free text",
    }
    p = await wd.watchdog_judge(CFG, _cand(judged_task.task_id))
    assert set(vars(p).values()) <= {WS, judged_task.task_id, 1, "judge", "steer", p.finding_id, 0.95}
    (f,) = await watchdog_repo.query_findings("20261104", "")
    assert "rm -rf" not in str(f)
    judged_task.decide.return_value = {"verdict": {"choice": "rm -rf", "probabilities": {"rm -rf": 1.0}}}
    assert await wd.watchdog_judge(CFG, _cand(judged_task.task_id)) is None


async def test_judge_caps_per_day_and_per_attempt(judged_task):
    cfg = WatchdogConfig(judge_per_day=2)
    assert await wd.watchdog_judge(cfg, _cand(judged_task.task_id)) is not None
    assert await wd.watchdog_judge(cfg, _cand(judged_task.task_id)) is not None
    assert await wd.watchdog_judge(cfg, _cand(judged_task.task_id)) is None
    assert ("watchdog.cap_hit", 1.0, {"Kind": "judge"}) in judged_task.metrics
    assert judged_task.decide.await_count == 2

    big = WatchdogConfig(judge_per_day=100)
    for _ in range(14):
        assert await wd.watchdog_judge(big, _cand(judged_task.task_id)) is not None
    assert len(await watchdog_repo.query_findings("20261104", "")) == 16
    assert await wd.watchdog_judge(big, _cand(judged_task.task_id)) is None  # 17th judgement of this attempt
    assert judged_task.decide.await_count == 16


async def test_judge_skips_task_that_moved_on(judged_task):
    judged_task.table.update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(judged_task.task_id)},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "idle"},
    )
    assert await wd.watchdog_judge(CFG, _cand(judged_task.task_id)) is None
    judged_task.decide.assert_not_awaited()
