"""Task routes (wave2-O Task 8, C15) over a moto `tasks` table; Temporal, the chat gate and the task room are faked."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from temporalio.client import WorkflowUpdateFailedError

from core import tasks_schema as ts
from core.auth import AuthContext, get_current_user
from core.repositories import messaging_repo, tasks_repo
from core.services import chat_gate, runtime_runs, super_agent_flags, task_conversation
from core.temporal import client as temporal_client
from core.temporal.types import ActionRequest, StopRequest, StopResult
from routers import tasks as router_mod

from .test_tasks_repo import table  # noqa: F401

WS = "org_1"
SAM, PRIYA = "user_sam", "user_priya"
ROOM = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(super_agent_flags, "flags_for", flag)
    monkeypatch.setattr(router_mod, "human_display_name", AsyncMock(side_effect=lambda u: u))
    fakes = {
        "update": AsyncMock(return_value=None),
        "start": AsyncMock(),
        "handle": MagicMock(signal=AsyncMock()),
        "gate": AsyncMock(return_value={"blocked": False}),
        "room": AsyncMock(return_value="room-x"),
        "member": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(temporal_client, "update_task", fakes["update"])
    monkeypatch.setattr(temporal_client, "start_task_workflow", fakes["start"])
    monkeypatch.setattr(temporal_client, "task_handle", lambda ws, tid: fakes["handle"])
    monkeypatch.setattr(chat_gate, "gate_chat", fakes["gate"])
    monkeypatch.setattr(task_conversation, "ensure_task_room", fakes["room"])
    monkeypatch.setattr(messaging_repo, "get_member", fakes["member"])
    who = {"user": SAM}
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=who["user"], org_id=WS)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return {"client": client, "who": who, "flag": flag, **fakes}


async def _task(status="queued", audience=None, **over) -> str:
    row, _ = await tasks_repo.create_task(
        WS,
        {
            "taskId": ts.new_task_id(),
            "identifier": "LB-1",
            "title": "Draft the memo",
            "status": "proposed" if status == "proposed" else "queued",
            "audience": audience or {"kind": "person", "userIds": [SAM]},
            "lane": "private",
            "ownerUserId": SAM,
            "triggerId": ts.trigger_id("chat", ts.new_task_id()),
        }
        | over,
    )
    if status not in ("proposed", "queued"):
        tasks_repo._table().update_item(
            Key={"pk": row["pk"], "sk": row["sk"]},
            UpdateExpression="SET #s = :s, statusKey = :k",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":k": ts.status_key(WS, status)},
        )
    return row["taskId"]


async def test_list_scope_mine_filters_by_can_view(world):
    mine = await _task()
    await _task(audience={"kind": "person", "userIds": [PRIYA]}, ownerUserId=PRIYA)
    resp = await world["client"].get("/api/v1/tasks", params={"scope": "mine"})
    assert resp.status_code == 200
    assert [t["taskId"] for t in resp.json()["tasks"]] == [mine]
    assert resp.json()["next"] is None


async def test_list_scope_room_requires_membership(world):
    in_room = await _task(audience={"kind": "room", "roomId": ROOM})
    await _task()
    resp = await world["client"].get("/api/v1/tasks", params={"scope": f"room:{ROOM}"})
    assert resp.status_code == 403
    world["member"].return_value = {"memberId": SAM}
    resp = await world["client"].get("/api/v1/tasks", params={"scope": f"room:{ROOM}"})
    assert [t["taskId"] for t in resp.json()["tasks"]] == [in_room]


async def test_get_task_404_for_non_viewer(world):
    task = await _task()
    world["who"]["user"] = PRIYA
    resp = await world["client"].get(f"/api/v1/tasks/{task}")
    assert resp.status_code == 404  # same answer as a missing task: no existence leak
    world["room"].assert_not_awaited()


async def test_task_detail_seats_viewer_in_task_room(world):
    task = await _task()
    resp = await world["client"].get(f"/api/v1/tasks/{task}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["taskId"] == task and body["taskRoomId"] == "room-x"
    assert set(body) == {"task", "attempts", "questions", "effects", "subscriptions", "taskRoomId"}
    assert world["room"].await_args.kwargs == {"seat_viewer": SAM}


async def test_stop_returns_summary_and_attributes_user(world):
    task = await _task("working")
    world["update"].return_value = StopResult("stopping")
    resp = await world["client"].post(f"/api/v1/tasks/{task}/stop", json={"version": 1})
    assert resp.status_code == 200 and resp.json()["taskId"] == task
    world["update"].assert_awaited_once_with(WS, task, "stop", StopRequest(by_user_id=SAM))


async def test_start_409_unless_proposed(world):
    queued = await _task()
    resp = await world["client"].post(f"/api/v1/tasks/{queued}/start", json={"version": 1})
    assert resp.status_code == 409
    proposed = await _task("proposed")
    resp = await world["client"].post(f"/api/v1/tasks/{proposed}/start", json={"version": 1})
    assert resp.status_code == 200
    world["start"].assert_awaited_once_with(WS, proposed, "propose_start")
    world["handle"].signal.assert_awaited_once_with("start")


async def test_version_mismatch_409(world):
    task = await _task("idle")
    resp = await world["client"].post(f"/api/v1/tasks/{task}/resolve", json={"version": 7})
    assert resp.status_code == 409
    world["update"].assert_not_awaited()
    # an illegal state the workflow's validator refuses is a 409 too
    world["update"].side_effect = WorkflowUpdateFailedError(RuntimeError("resolve needs a task that is not running"))
    resp = await world["client"].post(f"/api/v1/tasks/{task}/resolve", json={"version": 1})
    assert resp.status_code == 409
    assert world["update"].await_args.args == (WS, task, "resolve", ActionRequest(SAM, expected_version=1))


async def test_temporal_down_503_retryable(world):
    task = await _task("failed")
    world["update"].side_effect = temporal_client.TemporalUnavailable("down")
    resp = await world["client"].post(f"/api/v1/tasks/{task}/dismiss", json={"version": 1})
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"code": "TEMPORAL_UNAVAILABLE", "retryable": True}


async def test_retry_runs_gate_chat(world):
    task = await _task("failed")
    copy = "You've used today's free allowance. It resets tomorrow."
    world["gate"].return_value = {"blocked": True, "code": "trial_daily_cap", "message": copy}
    resp = await world["client"].post(f"/api/v1/tasks/{task}/retry", json={"version": 1})
    assert resp.status_code == 402
    assert resp.json()["detail"] == {"code": "trial_daily_cap", "message": copy}
    world["update"].assert_not_awaited()
    world["gate"].return_value = {"blocked": False}
    resp = await world["client"].post(f"/api/v1/tasks/{task}/retry", json={"version": 1})
    assert resp.status_code == 200
    world["update"].assert_awaited_once_with(WS, task, "retry", ActionRequest(SAM, expected_version=1))


async def test_bulk_start_skips_non_viewable(world):
    mine = await _task("proposed")
    theirs = await _task("proposed", audience={"kind": "person", "userIds": [PRIYA]}, ownerUserId=PRIYA)
    resp = await world["client"].post("/api/v1/tasks/start", json={"taskIds": [mine, theirs]})
    assert resp.status_code == 200
    assert [t["taskId"] for t in resp.json()["tasks"]] == [mine]
    world["start"].assert_awaited_once_with(WS, mine, "propose_start")


async def test_trace_returns_recent_tools(world, monkeypatch):
    task = await _task()
    await tasks_repo.put_attempt(
        WS, task, 1, {"agentId": "a-1", "sessionId": "s", "status": "running", "startedAt": "2026-11-04T00:00:00Z"}
    )
    tools = [{"name": "web_search", "status": "done"}]
    get_run = AsyncMock(return_value=runtime_runs.RunStatus("r", "running", None, None, None, tools))
    monkeypatch.setattr(runtime_runs, "get_run", get_run)
    resp = await world["client"].get(f"/api/v1/tasks/{task}/trace", params={"attempt": 1})
    assert resp.status_code == 200
    assert resp.json() == {"attempt": 1, "recentTools": tools}
    identity = get_run.await_args.args[1]
    assert (identity.run_id, identity.attempt, identity.agent_id) == (ts.run_id(WS, task, 1), 1, "a-1")


async def test_routes_404_when_switch_off(world):
    task = await _task("proposed")
    world["flag"].return_value = False
    c = world["client"]
    for resp in (
        await c.get("/api/v1/tasks", params={"scope": "mine"}),
        await c.get(f"/api/v1/tasks/{task}"),
        await c.post(f"/api/v1/tasks/{task}/start", json={"version": 1}),
        await c.post("/api/v1/tasks/start", json={"taskIds": [task]}),
        await c.get(f"/api/v1/tasks/{task}/trace"),
        await c.post(f"/api/v1/tasks/{task}/read"),
    ):
        assert resp.status_code == 404
    world["start"].assert_not_awaited()
