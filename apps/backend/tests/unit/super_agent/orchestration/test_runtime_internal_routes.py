"""Run events and inputs routes (O Task 3; C13): hook-token auth, terminal once, progress line, dedupe."""

import base64

import httpx
import pytest
from fastapi import FastAPI

from core import tasks_schema as ts
from core.repositories import tasks_repo as repo
from routers import runtime_internal as ri

from .test_activities_task import WS, _attempt, _task, world  # noqa: F401 -- fixtures and helpers
from .test_tasks_repo import table  # noqa: F401

TOKEN = "hook-secret"


class FakeActivityHandle:
    def __init__(self, log):
        self.log = log

    async def heartbeat(self, *details):
        self.log.append(("heartbeat", details))

    async def complete(self, result):
        self.log.append(("complete", result))

    async def report_cancellation(self, *details):
        self.log.append(("cancelled", details))


class FakeWorkflowHandle:
    def __init__(self, log, wid):
        self.log, self.wid = log, wid

    async def signal(self, name, *args):
        self.log.append(("signal", self.wid, name, args))


class FakeTemporal:
    def __init__(self):
        self.log = []

    def get_async_activity_handle(self, task_token):
        assert task_token == b"tok"
        return FakeActivityHandle(self.log)

    def get_workflow_handle(self, wid):
        return FakeWorkflowHandle(self.log, wid)

    async def start_workflow(self, workflow, arg, **kw):
        self.log.append(("start", workflow, kw.get("start_signal"), kw.get("start_signal_args")))


@pytest.fixture
def api(world, monkeypatch):  # noqa: F811
    fake = FakeTemporal()
    monkeypatch.setattr(ri.temporal, "get_client", lambda: fake)

    async def container(owner_id):
        return {"hook_token": TOKEN} if owner_id == WS else None

    monkeypatch.setattr("core.services.container_auth.container_repo.get_by_owner_id", container)
    app = FastAPI()
    app.include_router(ri.router, prefix="/api/v1/internal/runtime")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    client.fake = fake
    client.world = world
    return client


H = {"Authorization": f"Bearer {TOKEN}"}


async def _running(tid):
    ref = await _attempt(tid)
    await repo.update_attempt(
        WS,
        tid,
        1,
        {"taskToken": base64.b64encode(b"tok").decode(), "status": "running"},
        allowed_status=frozenset({"dispatched"}),
    )
    return ref


def _url(ws, run_id):
    return f"/api/v1/internal/runtime/{ws}/runs/{run_id}/events"


async def test_run_event_terminal_completes_activity_once(api):
    tid = await _task()
    ref = await _running(tid)
    body = {
        "taskId": tid,
        "attempt": 1,
        "kind": "terminal",
        "status": "completed",
        "output": "Paid.",
        "usage": {"input_tokens": 9, "output_tokens": 3, "cost_usd": 0.01},
    }
    r1 = await api.post(_url(WS, ref.run_id), json=body, headers=H)
    r2 = await api.post(_url(WS, ref.run_id), json=body, headers=H)
    assert r1.status_code == r2.status_code == 200 and r2.json()["replayed"] is True
    completes = [e for e in api.fake.log if e[0] == "complete"]
    assert len(completes) == 1 and completes[0][1].outcome == "completed"
    att = await repo.get_attempt(WS, tid, 1)
    assert att["output"] == "Paid." and att["usage"]["inputTokens"] == 9
    assert "Paid." not in repr(api.fake.log)  # text never rides a payload


async def test_run_event_heartbeat_updates_progress_line(api):
    tid = await _task()
    ref = await _running(tid)
    body = {
        "taskId": tid,
        "attempt": 1,
        "kind": "heartbeat",
        "status": "running",
        "recentTools": [{"name": "web_search"}],
    }
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    body["recentTools"] = [{"name": "terminal"}]
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    lines = [p for p in api.world["posts"] if p.get("progress")]
    assert len(lines) == 1 and lines[0]["room"] == ts.task_room_id(tid)
    assert (await repo.get_attempt(WS, tid, 1))["recentTools"] == [{"name": "terminal"}]
    assert [e[0] for e in api.fake.log] == ["heartbeat", "heartbeat"]


async def test_run_event_wrong_owner_403(api, monkeypatch):
    tid = await _task()
    ref = await _running(tid)

    async def container(owner_id):
        return {"hook_token": TOKEN}

    monkeypatch.setattr("core.services.container_auth.container_repo.get_by_owner_id", container)
    body = {"taskId": tid, "attempt": 1, "kind": "terminal", "status": "completed"}
    r = await api.post(_url("org_other", ref.run_id), json=body, headers=H)
    assert r.status_code == 403


async def test_run_event_requires_hook_token(api):
    tid = await _task()
    ref = await _running(tid)
    body = {"taskId": tid, "attempt": 1, "kind": "terminal", "status": "completed"}
    assert (await api.post(_url(WS, ref.run_id), json=body)).status_code == 401
    assert (
        await api.post(_url(WS, ref.run_id), json=body, headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_inputs_route_dedupes_delivery_id(api):
    tid = await _task()
    body = {
        "taskId": tid,
        "sessionKey": ts.worker_session_key("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", tid),
        "kind": "job_completed",
        "jobId": "j1",
        "text": "build finished",
        "deliveryId": "d-1",
    }
    r1 = await api.post(f"/api/v1/internal/runtime/{WS}/inputs", json=body, headers=H)
    r2 = await api.post(f"/api/v1/internal/runtime/{WS}/inputs", json=body, headers=H)
    assert (r1.status_code, r2.status_code) == (202, 200)
    trig = await repo.get_trigger(WS, "job:d-1")
    assert trig["ref"]["text"] == "build finished"
    starts = [e for e in api.fake.log if e[0] == "start"]
    assert starts[0][2] == "input" and starts[0][3][0].trigger_id == "job:d-1"
