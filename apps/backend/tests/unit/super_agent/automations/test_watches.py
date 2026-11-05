"""Task watches on workflow timers (lane A, Task 12): met wakes the task, give-up asks or resolves, poke rechecks."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from temporalio import activity
from temporalio.worker import Worker

from core import tasks_schema as ts
from core.repositories import tasks_repo
from core.temporal import watches
from core.temporal.task_questions import AskRef, QuestionRef
from core.temporal.types import TaskStart
from core.temporal.watches import WatchSpec, check_watch, set_watch_until, watch_gave_up, watch_met
from core.temporal.workflows import WORKFLOWS, TaskWorkflow
from tests.unit.super_agent.orchestration.fake_activities import Fakes

WS, TID = "org_1", "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
PR = {"repo": "acme/site", "number": 12}


class WatchFakes:
    """The watch Activities (and Q's two an ask needs) under their real names; `checks` scripts check_watch."""

    def __init__(self, checks: list[str]):
        self.checks = list(checks)
        self.calls: list[tuple] = []
        self.until: list[str | None] = []

        self.gate: asyncio.Event | None = None  # set: the next check_watch holds until the test opens it
        self.checking = asyncio.Event()

        @activity.defn(name="check_watch")
        async def check(ws: str, task_id: str, spec: WatchSpec) -> str:
            self.calls.append(("check_watch", spec.watch_id, activity.info().scheduled_time))
            if self.gate is not None:
                gate, self.gate = self.gate, None
                self.checking.set()
                await gate.wait()
            return self.checks.pop(0) if self.checks else "not_met"

        @activity.defn(name="watch_met")
        async def met(ws: str, task_id: str, spec: WatchSpec, n: int, result: str) -> str:
            self.calls.append(("watch_met", spec.watch_id, n, result))
            return f"watch:{task_id}:{spec.watch_id}:{n}"

        @activity.defn(name="watch_gave_up")
        async def gave_up(ws: str, task_id: str, spec: WatchSpec) -> AskRef:
            self.calls.append(("watch_gave_up", spec.watch_id))
            return AskRef("ask:q-1", "question", "question", False)

        @activity.defn(name="set_watch_until")
        async def until(ws: str, task_id: str, until: str | None) -> None:
            self.until.append(until)

        @activity.defn(name="open_asked_question")
        async def open_q(ws: str, task_id: str, ref: AskRef) -> QuestionRef:
            self.calls.append(("open_asked_question", ref.trigger_id))
            return QuestionRef(ref.trigger_id.removeprefix("ask:"), ["user_sam"], False)

        @activity.defn(name="relay_question")
        async def relay(ws: str, task_id: str, qid: str) -> None:
            self.calls.append(("relay_question", qid))

        @activity.defn(name="cancel_open_questions")  # Q's, on Stop with a question open
        async def cancel(ws: str, task_id: str, by: str, reason: str) -> list[str]:
            self.calls.append(("cancel_open_questions", reason))
            return []

        @activity.defn(name="remind_question")  # Q's reminder, +24 h on an open question
        async def remind(ws: str, task_id: str, qid: str) -> None:
            self.calls.append(("remind_question", qid))

        self.activities = [check, met, gave_up, until, open_q, relay, cancel, remind]


@asynccontextmanager
async def _worker(env, fakes: Fakes, wf: WatchFakes):
    queue = f"q-{uuid.uuid4()}"
    fast = timedelta(milliseconds=50)
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=WORKFLOWS,
        activities=[*fakes.activities(), *wf.activities],
        max_heartbeat_throttle_interval=fast,
        default_heartbeat_throttle_interval=fast,
    ):
        yield queue


async def _until(pred, n=400):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def _spec(every=30, give_up_h=24.0, on_give_up="ask", kind="pr_checks", start=None) -> WatchSpec:
    base = start or datetime.now(timezone.utc)
    return WatchSpec(
        watch_id="w1",
        kind=kind,
        target=PR if kind == "pr_checks" else {"url": "https://x.test/ready"},
        every_min=every,
        give_up_at=(base + timedelta(hours=give_up_h)).isoformat(),
        on_give_up=on_give_up,
    )


async def _idle_task(env, queue):
    """A task whose first attempt completed: idle, waiting for inputs (where a watch is added)."""
    h = await env.client.start_workflow(
        TaskWorkflow.run, TaskStart(WS, TID, "router"), id=f"task:{WS}:{uuid.uuid4()}", task_queue=queue
    )
    return h


# --- the workflow -------------------------------------------------------------------------------------------


async def test_pr_checks_met_wakes_task(temporal_env):
    fakes, wf = Fakes(), WatchFakes(["not_met", "met:success"])
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        assert await h.execute_update("add_watch", _spec(every=30, start=start)) == "w1"
        await _until(lambda: len([c for c in wf.calls if c[0] == "check_watch"]) == 1)  # the first check, at once
        await temporal_env.sleep(timedelta(minutes=31))  # the time-skipping clock moves only when asked
        await _until(lambda: fakes.names().count("begin_attempt") == 2)  # woken by the watch
        await h.result()
    checks = [c for c in wf.calls if c[0] == "check_watch"]
    assert len(checks) == 2
    assert checks[1][2] - checks[0][2] >= timedelta(minutes=29)  # the second check waited every_min
    assert ("watch_met", "w1", 2, "met:success") in wf.calls
    # the task ran again with the watch input, and the row's watchUntil was set then cleared
    second = fakes.args("begin_attempt")[1]
    assert second[2] == [f"watch:{TID}:w1:2"]
    assert wf.until[0] == _spec(start=start).give_up_at and wf.until[-1] is None


async def test_idle_watching_sets_watch_until(temporal_env):
    fakes, wf = Fakes(), WatchFakes(["not_met", "not_met", "not_met"])
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", _spec(every=60, give_up_h=2.0, start=start))
        await _until(lambda: bool(wf.until))
        await h.signal("unwatch", "w1")
        await _until(lambda: len(wf.until) >= 2)
        assert wf.until == [_spec(give_up_h=2.0, start=start).give_up_at, None]
        await h.execute_update("stop", __import__("core.temporal.types", fromlist=["StopRequest"]).StopRequest("sam"))
        await h.result()


async def test_poke_rechecks_now(temporal_env):
    fakes, wf = Fakes(), WatchFakes(["not_met", "met"])
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", _spec(every=720, start=start))  # 12 h apart
        await _until(lambda: len([c for c in wf.calls if c[0] == "check_watch"]) == 1)
        await h.signal("watch_poke", "w1")
        await _until(lambda: fakes.names().count("begin_attempt") == 2)
        await h.result()
    checks = [c for c in wf.calls if c[0] == "check_watch"]
    assert len(checks) == 2 and checks[1][2] - checks[0][2] < timedelta(hours=1)  # not the 12 h later


async def test_give_up_asks(temporal_env):
    fakes, wf = Fakes(), WatchFakes(["not_met"] * 10)
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", _spec(every=30, give_up_h=1.0, start=start))
        await _until(lambda: len([c for c in wf.calls if c[0] == "check_watch"]) == 1)
        for _ in range(3):  # 30-minute checks until the hour is up, then the ask
            await temporal_env.sleep(timedelta(minutes=31))
            await asyncio.sleep(0.2)
        await _until(lambda: ("open_asked_question", "ask:q-1") in wf.calls)
        assert ("watch_gave_up", "w1") in wf.calls
        assert ("relay_question", "q-1") in wf.calls
        # the task now sits in Q's wait, not the 7-day quiet one: a reminder at +24 h, no resolve at day 8
        await temporal_env.sleep(timedelta(hours=25))
        await _until(lambda: ("remind_question", "q-1") in wf.calls)
        await temporal_env.sleep(timedelta(days=8))
        await asyncio.sleep(0.5)
        assert "transition" not in fakes.names()  # the task waits on the person, it did not end
        await h.execute_update("stop", __import__("core.temporal.types", fromlist=["StopRequest"]).StopRequest("sam"))
        await h.result()
    assert len([c for c in wf.calls if c[0] == "check_watch"]) <= 3  # checks at 0, 30, 60 min, then the ask


async def test_question_work_does_not_wait_behind_a_watch(temporal_env):
    """An approval asked while a watch runs: its +24 h reminder goes out and, once approved, the draft lands at
    once (not when the watch ends), while the watch keeps checking."""
    from core.temporal.task_questions import AnswerOut, AnswerRef, LandResult

    fakes, wf = Fakes(), WatchFakes(["not_met"] * 10)
    landed: list[str] = []

    @activity.defn(name="record_answer")
    async def record(ws: str, task_id: str, ref: AnswerRef) -> AnswerOut:
        return AnswerOut(f"answer:{ref.qid}", ref.option_id, land_idem_key="eff-1")

    @activity.defn(name="apply_effect")
    async def apply(ws: str, task_id: str, idem: str, by: str) -> LandResult:
        landed.append(idem)
        return LandResult("done")

    wf.activities += [record, apply]
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", _spec(every=720, give_up_h=72.0, start=start))
        await _until(lambda: len([c for c in wf.calls if c[0] == "check_watch"]) == 1)
        assert await h.execute_update("ask", AskRef("ask:q-1", "approval", "approval", True)) == "q-1"
        await temporal_env.sleep(timedelta(hours=25))
        await _until(lambda: ("remind_question", "q-1") in wf.calls)
        assert len([c for c in wf.calls if c[0] == "check_watch"]) >= 2  # the watch kept checking meanwhile
        await h.execute_update("answer", AnswerRef("q-1", "approve", None, "user_sam", "ca-1"))
        await _until(lambda: landed == ["eff-1"])  # no clock moved: it landed while the watch still runs
        await h.execute_update("stop", __import__("core.temporal.types", fromlist=["StopRequest"]).StopRequest("sam"))
        await h.result()


async def test_give_up_fail(temporal_env):
    fakes, wf = Fakes(), WatchFakes(["not_met"] * 10)
    start = await temporal_env.get_current_time()
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", _spec(every=30, give_up_h=1.0, on_give_up="fail", start=start))
        await h.result()  # the task ends on give-up, so the result lets the clock skip
    # failed is not reachable from idle; the task ends there, it does not sit 7 days and resolve quiet
    assert fakes.args("transition") == [(WS, TID, "resolved", "watch_gave_up")]
    assert ("watch_gave_up", "w1") not in wf.calls


async def test_give_up_fail_lands_approved_drafts_and_cancels_open_questions(temporal_env):
    """An answer lands while one watch is being checked and another gives up (`fail`) in the same pass: the
    approved draft still lands, the Question left open is cancelled, and only then does the task resolve."""
    import dataclasses

    from core.temporal.task_questions import AnswerOut, AnswerRef, LandResult

    fakes, wf = Fakes(), WatchFakes(["not_met"] * 10)
    landed: list[str] = []

    @activity.defn(name="record_answer")
    async def record(ws: str, task_id: str, ref: AnswerRef) -> AnswerOut:
        return AnswerOut(f"answer:{ref.qid}", ref.option_id, land_idem_key="eff-1")

    @activity.defn(name="apply_effect")
    async def apply(ws: str, task_id: str, idem: str, by: str) -> LandResult:
        landed.append(idem)
        wf.calls.append(("apply_effect", idem))
        return LandResult("done")

    wf.activities += [record, apply]
    start = await temporal_env.get_current_time()
    slow = _spec(every=60, give_up_h=72.0, on_give_up="fail", start=start)
    soon = dataclasses.replace(_spec(every=60, give_up_h=1.0, on_give_up="fail", start=start), watch_id="w2")
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await _until(lambda: "end_attempt" in fakes.names())
        await h.execute_update("add_watch", slow)
        await h.execute_update("add_watch", soon)
        await _until(lambda: len([c for c in wf.calls if c[0] == "check_watch"]) == 2)
        await h.execute_update("ask", AskRef("ask:a-1", "approval", "approval", True))
        await h.execute_update("ask", AskRef("ask:a-2", "approval", "approval", True))
        gate = wf.gate = asyncio.Event()
        # both due at +60 min: w1's check holds there (the clock stops while it runs), w2 is past its give-up
        sleeper = asyncio.create_task(temporal_env.sleep(timedelta(minutes=61)))
        await _until(wf.checking.is_set)
        await h.execute_update("answer", AnswerRef("a-1", "approve", None, "user_sam", "ca-1"))
        gate.set()
        await h.result()
        sleeper.cancel()
    assert landed == ["eff-1"]
    names = [c[0] for c in wf.calls]
    assert names.index("apply_effect") < names.index("cancel_open_questions")
    assert ("cancel_open_questions", "watch_gave_up") in wf.calls
    assert fakes.args("transition")[-1] == (WS, TID, "resolved", "watch_gave_up")


async def test_no_watch_changes_nothing(temporal_env):
    fakes, wf = Fakes(), WatchFakes([])
    async with _worker(temporal_env, fakes, wf) as q:
        h = await _idle_task(temporal_env, q)
        await h.result()
    assert wf.calls == [] and wf.until == []
    assert fakes.names() == [
        "check_budget",
        "acquire_slot",
        "begin_attempt",
        "worker_turn",
        "end_attempt",
        "close_automation_run",
        "transition",
    ]


# --- the Activities -----------------------------------------------------------------------------------------


def _resp(status, body=None):
    return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request("GET", "https://x/"))


def _http(responses: dict[str, httpx.Response]):
    client = MagicMock()

    async def get(url, **kw):
        return next(r for key, r in responses.items() if key in url)

    client.get = AsyncMock(side_effect=get)

    @asynccontextmanager
    async def fake(*a, **k):
        yield client

    return patch("httpx.AsyncClient", new=fake)


def _url_http(response: httpx.Response | None, *, refuse: bool = False):
    """The SSRF-safe path: `link_preview.pinned_request` builds the request, the client sends it."""
    from core.services import link_preview

    client = MagicMock()
    client.build_request = lambda method, url, **kw: httpx.Request(method, url)
    client.send = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake(*a, **k):
        yield client

    async def pinned(http, method, url, **kw):
        if refuse:
            raise link_preview.Refused("non-public address")
        return http.build_request(method, url)

    return patch("httpx.AsyncClient", new=fake), patch("core.services.link_preview.pinned_request", pinned), client


async def test_check_watch_url_goes_through_the_ssrf_safe_fetch():
    a, b, client = _url_http(_resp(200))
    with a, b:
        assert await check_watch(WS, TID, _spec(kind="url")) == "met"
    client.send.assert_awaited_once()
    a, b, _ = _url_http(_resp(503))
    with a, b:
        assert await check_watch(WS, TID, _spec(kind="url")) == "not_met"
    a, b, client = _url_http(None, refuse=True)
    with a, b:
        assert await check_watch(WS, TID, _spec(kind="url")) == "error"  # a private host is never fetched
    assert not client.send.called
    with patch("httpx.AsyncClient", side_effect=RuntimeError("dns")):
        assert await check_watch(WS, TID, _spec(kind="url")) == "error"


def test_at_reads_a_naive_time_as_utc():
    assert watches._at("2027-01-01T00:00:00").tzinfo is not None
    assert watches._at("2027-01-01T00:00:00+02:00").utcoffset() == timedelta(hours=2)


async def test_check_watch_pr_checks():
    pulls = _resp(200, {"head": {"sha": "abc"}})
    with patch("core.services.inbound_triggers._github_token", AsyncMock(return_value="gh")):
        with _http(
            {"pulls": pulls, "/status": _resp(200, {"state": "pending"}), "check-runs": _resp(200, {"check_runs": []})}
        ):
            assert await check_watch(WS, TID, _spec()) == "not_met"
        running = {"check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}]}
        with _http({"pulls": pulls, "/status": _resp(200, {"state": "success"}), "check-runs": _resp(200, running)}):
            assert await check_watch(WS, TID, _spec()) == "not_met"
        done = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}
        with _http({"pulls": pulls, "/status": _resp(200, {"state": "success"}), "check-runs": _resp(200, done)}):
            assert await check_watch(WS, TID, _spec()) == "met:success"
        failed = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}
        with _http({"pulls": pulls, "/status": _resp(200, {"state": "failure"}), "check-runs": _resp(200, failed)}):
            assert await check_watch(WS, TID, _spec()) == "met:failed:ci"
    with patch("core.services.inbound_triggers._github_token", AsyncMock(return_value=None)):
        assert await check_watch(WS, TID, _spec()) == "error"


async def _task(table):
    with patch("core.repositories.messaging_repo.ensure_workspace_id", AsyncMock(return_value="ws")):
        await tasks_repo.create_task(
            WS,
            {
                "taskId": TID,
                "identifier": "LB-1",
                "title": "Ship it",
                "status": "queued",
                "audience": {"kind": "workspace"},
                "lane": "shared",
                "triggerId": "chat:m1",
            },
        )


async def test_watch_met_writes_the_input_trigger(table):
    await _task(table)
    tid = await watch_met(WS, TID, _spec(), 3, "met:failed:ci")
    assert tid == f"watch:{TID}:w1:3"
    row = await tasks_repo.get_trigger(WS, tid)
    assert row["source"] == "watch" and row["lane"] == "shared"
    assert row["ref"]["text"] == "Watch met: PR acme/site#12 checks finished (failed:ci)."
    assert await watch_met(WS, TID, _spec(), 3, "met:failed:ci") == tid  # a retry is the same row


async def test_watch_gave_up_writes_the_ask_row(table):
    await _task(table)
    ref = await watch_gave_up(WS, TID, _spec())
    assert ref.kind == "question" and ref.blocking is False
    row = await tasks_repo.get_trigger(WS, ref.trigger_id)
    assert row["source"] == "ask" and row["ref"]["options"] == watches.GIVE_UP_OPTIONS
    assert row["ref"]["title"].startswith("Still waiting on the checks on PR acme/site#12")
    assert row["ref"]["qid"] == ref.trigger_id.removeprefix("ask:")
    # "keep watching" moves the give-up time: the next give-up is a new question, not the answered one
    later = await watch_gave_up(WS, TID, _spec(give_up_h=48.0))
    assert later.trigger_id != ref.trigger_id


async def test_set_watch_until_updates_the_row(table):
    await _task(table)
    await set_watch_until(WS, TID, "2027-01-01T00:00:00+00:00")
    assert (await tasks_repo.get_task(WS, TID))["watchUntil"] == "2027-01-01T00:00:00+00:00"
    await set_watch_until(WS, TID, None)
    assert (await tasks_repo.get_task(WS, TID)).get("watchUntil") is None
    await set_watch_until(WS, "00000000-0000-4000-8000-000000000000", None)  # no such task: nothing


def test_watch_id_is_deterministic():
    a = watches.watch_id_for(TID, "pr_checks", {"number": 12, "repo": "acme/site"})
    assert a == watches.watch_id_for(TID, "pr_checks", {"repo": "acme/site", "number": 12})
    assert a != watches.watch_id_for(TID, "pr_checks", {"repo": "acme/site", "number": 13})


# --- the routes -----------------------------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    from main import app

    updates: list[tuple] = []
    signals: list[tuple] = []
    handle = MagicMock()
    handle.signal = AsyncMock(side_effect=lambda name, arg: signals.append((name, arg)))
    with (
        patch("routers.internal_automations._require_hook_token", AsyncMock(return_value={})),
        patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=True)),
        patch("routers.runtime_internal._attempt_of", AsyncMock(return_value={"n": 1})) as att,
        patch("routers.runtime_internal._current_task", AsyncMock(return_value={"taskId": TID})) as current,
        patch("core.temporal.client.update_task", AsyncMock(side_effect=lambda *a: updates.append(a))),
        patch("core.temporal.client.task_handle", lambda ws, tid: handle),
    ):
        yield type(
            "Api",
            (),
            {"client": TestClient(app), "updates": updates, "signals": signals, "attempt": att, "current": current},
        )()


_BODY = {"runId": "run-1", "attempt": 1, "runSecret": "s", "kind": "pr_checks", "target": PR}


def test_add_watch_route(api):
    r = api.client.post(f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=_BODY, headers={"Authorization": "Bearer t"})
    assert r.status_code == 202, r.text
    assert r.json()["watchId"] == watches.watch_id_for(TID, "pr_checks", PR)
    datetime.fromisoformat(r.json()["giveUpAt"])
    ((ws, tid, name, spec),) = api.updates
    assert (ws, tid, name) == (WS, TID, "add_watch")
    assert spec == WatchSpec(
        watches.watch_id_for(TID, "pr_checks", PR), "pr_checks", PR, 30, r.json()["giveUpAt"], "ask"
    )
    api.attempt.assert_awaited_once_with(WS, TID, 1, "run-1", "s")
    api.current.assert_awaited_once_with(WS, {"n": 1}, TID, 1)  # the task's current live attempt, as worker_ask


def test_add_watch_refuses_a_stale_attempt(api):
    from fastapi import HTTPException

    api.current.side_effect = HTTPException(status_code=409, detail="stale_run")
    r = api.client.post(f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=_BODY, headers={"Authorization": "Bearer t"})
    assert r.status_code == 409 and api.updates == []


def test_add_watch_give_up_must_be_aware_and_soon(api):
    naive = _BODY | {"giveUpAt": "2027-01-01T00:00:00"}
    r = api.client.post(f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=naive, headers={"Authorization": "Bearer t"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_give_up_at"
    far = _BODY | {"giveUpAt": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
    r = api.client.post(f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=far, headers={"Authorization": "Bearer t"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "give_up_too_far"
    assert api.updates == []
    soon = (datetime.now(timezone(timedelta(hours=2))) + timedelta(hours=3)).isoformat()
    r = api.client.post(
        f"/api/v1/internal/tasks/{WS}/{TID}/watches",
        json=_BODY | {"giveUpAt": soon},
        headers={"Authorization": "Bearer t"},
    )
    assert r.status_code == 202 and r.json()["giveUpAt"].endswith("+00:00")  # one offset: watchUntil's max() holds
    assert api.updates[0][3].give_up_at == r.json()["giveUpAt"]


def test_add_watch_validates(api):
    bad = _BODY | {"target": {"repo": "acme/site"}}  # no number
    assert (
        api.client.post(
            f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=bad, headers={"Authorization": "Bearer t"}
        ).status_code
        == 400
    )
    bad = _BODY | {"kind": "url", "target": {"url": "ftp://x"}}
    assert (
        api.client.post(
            f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=bad, headers={"Authorization": "Bearer t"}
        ).status_code
        == 400
    )
    bad = _BODY | {"everyMin": 1}
    assert (
        api.client.post(
            f"/api/v1/internal/tasks/{WS}/{TID}/watches", json=bad, headers={"Authorization": "Bearer t"}
        ).status_code
        == 422
    )
    assert api.updates == []


def test_remove_watch_route(api):
    body = {"runId": "run-1", "attempt": 1, "runSecret": "s", "watchId": "w1"}
    r = api.client.post(
        f"/api/v1/internal/tasks/{WS}/{TID}/watches/remove", json=body, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 204
    assert api.signals == [("unwatch", "w1")]


def test_watch_source_in_schema():
    assert "watch" in ts.TriggerSource.__args__
    assert ts.trigger_id("watch", TID, "w1", 2) == f"watch:{TID}:w1:2"
    schema = json.loads(
        (
            __import__("pathlib").Path(ts.__file__).parents[3]
            / "packages/shared/fixtures/super-agent/tasks-schema.json"
        ).read_text()
    )
    assert "watch" in schema["enums"]["TriggerSource"]
