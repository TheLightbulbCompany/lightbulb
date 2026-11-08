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


async def _secret(tid, n=1):
    return (await repo.get_attempt(WS, tid, n))["runSecret"]


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
        "runSecret": await _secret(tid),
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
        "runSecret": await _secret(tid),
        "taskId": tid,
        "attempt": 1,
        "kind": "heartbeat",
        "status": "running",
        "recentTools": [{"name": "web_search"}],
    }
    before = (await repo.get_attempt(WS, tid, 1))["version"]
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    # no version bump: the projector publishes no workspace-wide task.changed every 2 minutes
    assert (await repo.get_attempt(WS, tid, 1))["version"] == before
    body["recentTools"] = [{"name": "terminal"}]
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    lines = [p for p in api.world["posts"] if p.get("progress")]
    assert len(lines) == 1 and lines[0]["room"] == ts.task_room_id(tid)
    assert lines[0]["specialist"] == "finance"  # N-F6: posted by the specialist doing the work, not Lightbulb
    assert (await repo.get_attempt(WS, tid, 1))["recentTools"][0]["name"] == "terminal"
    assert [e[0] for e in api.fake.log] == ["heartbeat", "heartbeat"]


async def test_a_heartbeat_due_for_credentials_polls_the_run_so_its_invoke_carries_a_fresh_set(api, monkeypatch):
    """STS caps a set at 1 h; a run lasts up to 6.5 h and sends no invoke while it runs. A heartbeat
    saying the set is due gets one poll of that run (an invoke on its own session); one not due, none."""
    import asyncio

    polled = []

    async def get_run(owner_id, identity):
        polled.append((owner_id, identity.run_id, identity.task_id))

    monkeypatch.setattr(ri.runtime_runs, "get_run", get_run)
    tid = await _task()
    ref = await _running(tid)
    body = {"runSecret": await _secret(tid), "taskId": tid, "attempt": 1, "kind": "heartbeat", "status": "running"}
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    await api.post(_url(WS, ref.run_id), json={**body, "credentialsDue": True}, headers=H)
    await asyncio.gather(*ri._REFRESHES)
    assert polled == [(WS, ref.run_id, tid)]


async def test_progress_line_is_plain_words_and_skips_internal_tools(api):
    """Live dev bug (5 Nov): "Working: skill_view". An internal tool posts nothing; the first real one
    posts plain words, once per attempt."""
    tid = await _task()
    ref = await _running(tid)
    body = {"runSecret": await _secret(tid), "taskId": tid, "attempt": 1, "kind": "heartbeat", "status": "running"}
    await api.post(_url(WS, ref.run_id), json={**body, "recentTools": [{"name": "skill_view"}]}, headers=H)
    assert not [p for p in api.world["posts"] if p.get("progress")]
    await api.post(_url(WS, ref.run_id), json={**body, "recentTools": [{"name": "browser_navigate"}]}, headers=H)
    await api.post(_url(WS, ref.run_id), json={**body, "recentTools": [{"name": "terminal"}]}, headers=H)
    lines = [p for p in api.world["posts"] if p.get("progress")]
    assert [p["text"] for p in lines] == ["Using the browser…"]


async def test_terminal_stores_the_results_files(api):
    tid = await _task()
    ref = await _running(tid)
    files = [
        {"attachmentId": "4f1c1f2e-0000-4000-8000-000000000001", "fileName": "a.docx", "contentType": "application/pdf"}
    ]
    body = {
        "runSecret": await _secret(tid),
        "taskId": tid,
        "attempt": 1,
        "kind": "terminal",
        "status": "completed",
        "output": "Done.",
        "attachments": files,
    }
    assert (await api.post(_url(WS, ref.run_id), json=body, headers=H)).status_code == 200
    assert (await repo.get_attempt(WS, tid, 1))["attachments"] == files


async def test_terminal_attachments_capped_at_the_message_limit(api):
    from core.services.messaging import ATTACHMENT_MAX_COUNT

    tid = await _task()
    ref = await _running(tid)
    f = {"fileName": "a.docx", "contentType": "application/pdf"}
    files = [{**f, "attachmentId": f"4f1c1f2e-0000-4000-8000-00000000000{i}"} for i in range(ATTACHMENT_MAX_COUNT + 1)]
    body = {
        "runSecret": await _secret(tid),
        "taskId": tid,
        "attempt": 1,
        "kind": "terminal",
        "status": "completed",
        "output": "Done.",
        "attachments": files,
    }
    assert (await api.post(_url(WS, ref.run_id), json=body, headers=H)).status_code in (400, 422)


async def test_run_event_wrong_owner_403(api, monkeypatch):
    tid = await _task()
    ref = await _running(tid)

    async def container(owner_id):
        return {"hook_token": TOKEN}

    monkeypatch.setattr("core.services.container_auth.container_repo.get_by_owner_id", container)
    body = {"runSecret": await _secret(tid), "taskId": tid, "attempt": 1, "kind": "terminal", "status": "completed"}
    r = await api.post(_url("org_other", ref.run_id), json=body, headers=H)
    assert r.status_code == 403


async def test_run_event_requires_hook_token(api):
    tid = await _task()
    ref = await _running(tid)
    body = {"runSecret": await _secret(tid), "taskId": tid, "attempt": 1, "kind": "terminal", "status": "completed"}
    assert (await api.post(_url(WS, ref.run_id), json=body)).status_code == 401
    assert (
        await api.post(_url(WS, ref.run_id), json=body, headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_inputs_route_dedupes_delivery_id(api):
    tid = await _task()
    await _running(tid)
    body = {
        "taskId": tid,
        "sessionKey": ts.worker_session_key("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", tid),
        "kind": "job_completed",
        "jobId": "j1",
        "text": "build finished",
        "deliveryId": "d-1",
    }
    body["runSecret"] = await _secret(tid)
    r1 = await api.post(f"/api/v1/internal/runtime/{WS}/inputs", json=body, headers=H)
    r2 = await api.post(f"/api/v1/internal/runtime/{WS}/inputs", json=body, headers=H)
    assert (r1.status_code, r2.status_code) == (202, 200)
    trig = await repo.get_trigger(WS, "job:d-1")
    assert trig["ref"]["text"] == "build finished"
    starts = [e for e in api.fake.log if e[0] == "start"]
    assert starts[0][2] == "input" and starts[0][3][0].trigger_id == "job:d-1"


async def test_run_event_needs_the_attempts_secret(api):
    """Another runtime of the same owner holds the hook token but not this attempt's secret."""
    tid = await _task()
    ref = await _running(tid)
    body = {
        "runSecret": "forged",
        "taskId": tid,
        "attempt": 1,
        "kind": "terminal",
        "status": "completed",
        "output": "forged result",
    }
    assert (await api.post(_url(WS, ref.run_id), json=body, headers=H)).status_code == 403
    assert not [e for e in api.fake.log if e[0] == "complete"]


async def test_inputs_need_the_tasks_session_and_secret(api):
    tid = await _task()
    await _running(tid)
    good = {
        "taskId": tid,
        "sessionKey": ts.worker_session_key("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", tid),
        "kind": "job_completed",
        "jobId": "j1",
        "text": "x",
        "deliveryId": "d-2",
        "runSecret": await _secret(tid),
    }
    other = ts.worker_session_key("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", ts.new_task_id())
    url = f"/api/v1/internal/runtime/{WS}/inputs"
    assert (await api.post(url, json={**good, "sessionKey": other}, headers=H)).status_code == 403
    assert (await api.post(url, json={**good, "runSecret": "nope"}, headers=H)).status_code == 403
    assert (await api.post(url, json=good, headers=H)).status_code == 202


async def test_recent_tools_are_capped(api):
    tid = await _task()
    ref = await _running(tid)
    tools = [{"name": "t" * 500, "argsHash": "h", "ok": True, "result": "x" * 5000}] * 50
    body = {
        "runSecret": await _secret(tid),
        "taskId": tid,
        "attempt": 1,
        "kind": "heartbeat",
        "status": "running",
        "recentTools": tools,
    }
    await api.post(_url(WS, ref.run_id), json=body, headers=H)
    kept = (await repo.get_attempt(WS, tid, 1))["recentTools"]
    assert len(kept) == 20 and len(kept[0]["name"]) == 64 and "result" not in kept[0]


async def test_task_note_posts_in_its_own_task_room(api):
    """task_note (contracts-v2 §6): the current attempt posts in its task's room as Lightbulb · specialist; the same
    text twice is one post; a wrong secret or a stale attempt never posts."""
    tid = await _task()
    ref = await _running(tid)
    body = {"taskId": tid, "runId": ref.run_id, "attempt": 1, "runSecret": await _secret(tid), "text": "Found the PO."}
    url = f"/api/v1/internal/runtime/{WS}/notes"
    assert (await api.post(url, json=body, headers=H)).status_code == 200
    await api.post(url, json=body, headers=H)
    notes = [p for p in api.world["posts"] if p.get("kind") == "note"]
    assert {p["event_id"] for p in notes} == {notes[0]["event_id"]}
    assert notes[0]["room"] == ts.task_room_id(tid) and notes[0]["specialist"] == "finance"
    assert notes[0]["progress"] is True  # never mentions, notifies or goes into room memory
    for i in range(18):  # 2 counted above: the 21st note in one attempt is refused
        assert (await api.post(url, json={**body, "text": f"n{i}"}, headers=H)).status_code == 200
    capped = await api.post(url, json={**body, "text": "one more"}, headers=H)
    assert capped.status_code == 429 and capped.json()["detail"] == "note_limit"
    assert (await api.post(url, json={**body, "runSecret": "wrong"}, headers=H)).status_code == 403
    await repo.update_attempt(WS, tid, 1, {"status": "ended"}, allowed_status=frozenset({"running"}))
    stale = await api.post(url, json=body, headers=H)
    assert stale.status_code == 409 and stale.json()["detail"] == "stale_run"


async def test_front_desk_acts_only_for_a_callers_thread(api, monkeypatch):
    """Governance P2, the caller case: a caller's thread books through the phone's own route for its own call; a
    member's thread is refused."""
    from unittest.mock import AsyncMock

    from routers import voice_phone as vp

    call = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(vp.messaging_repo, "get_room", AsyncMock(return_value={"kind": "call", "ownerId": WS}))
    booked = AsyncMock(return_value={"booked": True})
    monkeypatch.setattr(vp, "book", booked)
    from core.services import business_profile

    forgotten = []
    monkeypatch.setattr(business_profile, "forget", forgotten.append)
    tid = await _task(origin="phone_contact", triggerId="voice:c1:r1")
    trig = {"voice:c1:r1": {"ref": {"origin": "phone_contact", "callId": call}}, "chat:m1": {"ref": {"text": "hi"}}}
    monkeypatch.setattr(repo, "get_trigger", AsyncMock(side_effect=lambda _ws, t: trig.get(t)))
    ref = await _running(tid)
    body = {"taskId": tid, "runId": ref.run_id, "attempt": 1, "runSecret": await _secret(tid), "action": "book",
            "start": "2026-11-14T14:00", "service": "Gel manicure", "name": "Ana"}  # fmt: skip
    url = f"/api/v1/internal/runtime/{WS}/front-desk"
    r = await api.post(url, json=body, headers=H)
    assert r.status_code == 200 and r.json() == {"booked": True}
    assert booked.await_args.args[0] == call and booked.await_args.args[1].start == "2026-11-14T14:00"
    assert forgotten == [WS]  # a book drops the cached Business profile
    bad = await api.post(url, json={**body, "start": "Saturday"}, headers=H)
    assert bad.status_code == 422

    member = await _task()
    mref = await _running(member)
    body |= {"taskId": member, "runId": mref.run_id, "runSecret": await _secret(member)}
    r = await api.post(url, json=body, headers=H)
    assert r.status_code == 403 and r.json()["detail"] == "not_a_caller_thread"


async def test_business_facts_reads_only_the_runs_own_workspace(api, monkeypatch):
    """business_facts (router-latency-2026-11-08): a Worker reads its own workspace's Agent page, never another's;
    a wrong secret or another workspace's path reads nothing."""
    from core.repositories import messaging_repo

    pages = {WS: {"services": [{"name": "Cut", "price": "$40", "minutes": 30}],
                  "knowledge": [{"q": "Parking?", "a": "Free out back."}]},
             "org_other": {"services": [{"name": "Other salon secret"}]}}  # fmt: skip
    seen = []

    async def page(owner_id):
        seen.append(owner_id)
        return pages.get(owner_id)

    monkeypatch.setattr(messaging_repo, "get_voice_agent", page)
    tid = await _task()
    ref = await _running(tid)
    body = {"taskId": tid, "runId": ref.run_id, "attempt": 1, "runSecret": await _secret(tid)}
    got = await api.post(f"/api/v1/internal/runtime/{WS}/business-facts", json=body, headers=H)
    assert got.status_code == 200
    facts = got.json()["facts"]
    assert "Cut $40 (30 min)" in facts and "Parking? A: Free out back." in facts and "Other" not in facts
    assert set(seen) == {WS}
    bad = await api.post(f"/api/v1/internal/runtime/{WS}/business-facts", json={**body, "runSecret": "x"}, headers=H)
    assert bad.status_code == 403
    other = await api.post("/api/v1/internal/runtime/org_other/business-facts", json=body, headers=H)
    assert other.status_code in (401, 403) and "org_other" not in seen
