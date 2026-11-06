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
            "status": "queued",
            "audience": audience or {"kind": "person", "userIds": [SAM]},
            "lane": "private",
            "ownerUserId": SAM,
            "triggerId": ts.trigger_id("chat", ts.new_task_id()),
        }
        | over,
    )
    if status != "queued":  # `proposed` too: a row stored before 5 Nov 2026
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


async def test_no_start_or_dismiss_and_legacy_proposed_reads_closed(world):
    """5 Nov 2026: requests always start; a row stored `proposed` before then lists as Closed (resolved)."""
    legacy = await _task("proposed")
    for verb in ("start", "dismiss"):
        resp = await world["client"].post(f"/api/v1/tasks/{legacy}/{verb}", json={"version": 1})
        assert resp.status_code == 422
    assert (await world["client"].post("/api/v1/tasks/start", json={"taskIds": [legacy]})).status_code in (404, 405)
    world["start"].assert_not_awaited()
    (row,) = (await world["client"].get("/api/v1/tasks", params={"scope": "mine"})).json()["tasks"]
    assert (row["taskId"], row["status"], row["statusReason"], row["shownGroup"]) == (
        legacy,
        "resolved",
        "dismissed",
        "resolved",
    )
    assert "proposalReason" not in row


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
    resp = await world["client"].post(f"/api/v1/tasks/{task}/resolve", json={"version": 1})
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
    assert identity.session_kind == "workspace"
    # A task-session attempt (V3) is read from its own session, or the trace comes back empty.
    from core.services import runtime_transport

    await tasks_repo.put_attempt(
        WS,
        task,
        2,
        {
            "agentId": "a-1",
            "sessionId": runtime_transport.session_id_for(WS, task),
            "status": "running",
            "startedAt": "2026-11-04T00:00:00Z",
        },
    )
    resp = await world["client"].get(f"/api/v1/tasks/{task}/trace", params={"attempt": 2})
    assert resp.status_code == 200 and get_run.await_args.args[1].session_kind == "task"


async def test_routes_404_when_switch_off(world):
    task = await _task()
    world["flag"].return_value = False
    c = world["client"]
    for resp in (
        await c.get("/api/v1/tasks", params={"scope": "mine"}),
        await c.get(f"/api/v1/tasks/{task}"),
        await c.post(f"/api/v1/tasks/{task}/stop", json={"version": 1}),
        await c.get(f"/api/v1/tasks/{task}/trace"),
        await c.post(f"/api/v1/tasks/{task}/read"),
    ):
        assert resp.status_code == 404
    world["start"].assert_not_awaited()


async def test_list_needs_you_is_complete_and_matches_shown_group(world):
    from core.repositories import questions_repo

    asked = await _task("waiting")
    await questions_repo.open_question(
        WS,
        {
            "taskId": asked,
            "qid": "q-1",
            "kind": "question",
            "title": "Which venue?",
            "text": "",
            "options": [],
            "askedBy": {"kind": "worker", "attempt": 1},
            "answerers": [SAM],
            "answererRule": "requester",
            "baseAnswerers": [SAM],
        },
    )
    failed_mine = await _task("failed")
    await _task(
        "failed", ownerUserId=PRIYA, audience={"kind": "workspace"}
    )  # someone else's failure: Done, not Needs you
    for _ in range(3):
        await _task("working")  # plain work never counts
    resp = await world["client"].get("/api/v1/tasks", params={"scope": "workspace", "needsYou": "true", "limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["next"] is None  # unpaged: the whole set, whatever `limit` says
    assert sorted(t["taskId"] for t in body["tasks"]) == sorted([asked, failed_mine])
    assert {t["shownGroup"] for t in body["tasks"]} == {"waiting_on_you"}


async def test_list_needs_you_scan_is_capped_and_logged(world, monkeypatch, caplog):
    endless = AsyncMock(return_value=([], "more"))  # a failed-task walk that never ends
    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", endless)
    with caplog.at_level("WARNING", logger=router_mod.__name__):
        resp = await world["client"].get("/api/v1/tasks", params={"scope": "workspace", "needsYou": "true"})
    assert resp.status_code == 200 and resp.json() == {"tasks": [], "next": None}
    assert endless.await_count == router_mod._NEEDS_YOU_PAGES
    assert endless.await_args.kwargs["owner_user_id"] == SAM  # filtered by owner in the query
    assert "failed capped" in caplog.text


async def test_outputs_lists_what_visible_tasks_made_with_filters(world):
    """UX slice 5 (Creations): every output of every task the viewer can see, any status, newest first; filters by
    kind, room and specialist; another person's task stays hidden."""
    done = await _task("resolved", specialist="engineering")
    in_room = await _task("idle", audience={"kind": "room", "roomId": ROOM}, specialist="growth")
    hidden = await _task("resolved", audience={"kind": "person", "userIds": [PRIYA]}, ownerUserId=PRIYA)
    await _task("resolved")  # made nothing
    await tasks_repo.add_task_output(WS, done, {"id": "app:a1", "kind": "app", "title": "Booking", "createdAt": "1"})
    await tasks_repo.add_task_output(
        WS, in_room, {"id": "file:f1", "kind": "document", "title": "n.md", "createdAt": "2"}
    )
    await tasks_repo.add_task_output(
        WS, in_room, {"id": "effect:e1", "kind": "mail", "title": "Send", "createdAt": "3"}
    )
    await tasks_repo.add_task_output(WS, hidden, {"id": "app:a2", "kind": "app", "title": "Secret", "createdAt": "4"})
    world["member"].return_value = {"memberId": SAM}  # Sam is in the room

    async def ids(**params):
        resp = await world["client"].get("/api/v1/tasks/outputs", params=params)
        assert resp.status_code == 200, resp.text
        assert resp.json()["next"] is None
        return [o["id"] for o in resp.json()["outputs"]]

    assert await ids() == ["effect:e1", "file:f1", "app:a1"]
    assert await ids(kind="app") == ["app:a1"]
    assert await ids(kind=["app", "mail"]) == ["effect:e1", "app:a1"]  # repeated: one read for several kinds
    assert await ids(roomId=ROOM) == ["effect:e1", "file:f1"]
    assert await ids(specialist="engineering") == ["app:a1"]
    (app,) = (await world["client"].get("/api/v1/tasks/outputs", params={"kind": "app"})).json()["outputs"]
    assert app == {
        "id": "app:a1",
        "kind": "app",
        "title": "Booking",
        "ref": {},
        "createdAt": "1",
        "taskId": done,
        "identifier": "LB-1",
        "taskTitle": "Draft the memo",
        "taskStatus": "resolved",
        "specialist": "engineering",
        "roomId": None,
    }
    assert (await world["client"].get("/api/v1/tasks/outputs", params={"kind": "nope"})).status_code == 422
    # and a Done task's summary carries them too
    (row,) = [
        t
        for t in (await world["client"].get("/api/v1/tasks", params={"scope": "mine"})).json()["tasks"]
        if t["taskId"] == done
    ]
    assert [o["id"] for o in row["outputs"]] == ["app:a1"]


async def test_close_and_reopen_leave_a_marker_by_name(world, monkeypatch):
    """UX slice 3: the task's transcript says who closed or reopened it; Retry leaves no person marker."""
    post = AsyncMock(return_value="m1")
    monkeypatch.setattr(task_conversation, "post_as_lightbulb", post)
    task = await _task("idle")
    assert (await world["client"].post(f"/api/v1/tasks/{task}/resolve", json={"version": 1})).status_code == 200
    assert (await world["client"].post(f"/api/v1/tasks/{task}/retry", json={"version": 1})).status_code == 200
    (call,) = post.await_args_list
    assert call.kwargs["text"] == f"Closed by {SAM}" and call.kwargs["kind"] == "marker" and call.kwargs["progress"]
    assert call.kwargs["event_id"] == f"task-mark:{task}:resolve:1"


@pytest.mark.parametrize("action", ["retry", "reopen"])
async def test_retry_and_reopen_are_refused_in_a_paused_project(world, monkeypatch, action):
    """UX slice 6: no new work starts in a paused project, Retry and Reopen included."""
    rooms = {ROOM: {"roomId": ROOM, "pausedAt": "2026-11-06T10:00:00Z"}}
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))
    world["member"].return_value = {"id": SAM, "kind": "human"}  # SAM sits in the project
    task = await _task("failed", audience={"kind": "room", "roomId": ROOM}, lane="shared")
    resp = await world["client"].post(f"/api/v1/tasks/{task}/{action}", json={"version": 1})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "project_paused"
    world["update"].assert_not_awaited()
    rooms[ROOM] = {"roomId": ROOM}  # resumed
    resp = await world["client"].post(f"/api/v1/tasks/{task}/{action}", json={"version": 1})
    assert resp.status_code == 200


async def test_q_finds_visible_tasks_and_outputs_by_name(world):
    """W12-3 (⌘K): `q` keeps tasks by title or identifier and outputs by title or file name; hidden tasks and
    their outputs stay out."""
    bakery = await _task("resolved", title="Bakery taglines")
    await _task(title="Something else")
    hidden = await _task(
        "resolved", title="Bakery secret", audience={"kind": "person", "userIds": [PRIYA]}, ownerUserId=PRIYA
    )
    await tasks_repo.add_task_output(
        WS,
        bakery,
        {"id": "file:f1", "kind": "document", "title": "Taglines", "ref": {"fileName": "bakery.md"}, "createdAt": "1"},
    )
    await tasks_repo.add_task_output(WS, bakery, {"id": "app:a1", "kind": "app", "title": "Menu", "createdAt": "2"})
    await tasks_repo.add_task_output(
        WS, hidden, {"id": "file:f2", "kind": "file", "title": "bakery.csv", "createdAt": "3"}
    )

    tasks = (await world["client"].get("/api/v1/tasks", params={"scope": "workspace", "q": "BAKERY"})).json()
    assert [t["taskId"] for t in tasks["tasks"]] == [bakery]
    outputs = (await world["client"].get("/api/v1/tasks/outputs", params={"q": "bakery"})).json()["outputs"]
    assert [o["id"] for o in outputs] == ["file:f1"]  # file:f2's task is Priya's alone
    menu = (await world["client"].get("/api/v1/tasks/outputs", params={"q": "menu"})).json()["outputs"]
    assert [o["id"] for o in menu] == ["app:a1"]


async def test_q_reads_fixed_pages_and_stops_at_limit(world, monkeypatch):
    """A search reads full pages (no shrinking re-reads), reads trees for `limit` tasks only, and has no `next`."""
    for n in range(8):
        await _task(title=f"Bakery {n}")
    pages = []
    real = tasks_repo.list_tasks_by_status

    async def spy(ws, status, **kw):
        pages.append(kw["limit"])
        return await real(ws, status, **kw)

    trees = []
    real_tree = tasks_repo.get_task_tree

    async def tree_spy(ws, task_id):
        trees.append(task_id)
        return await real_tree(ws, task_id)

    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", spy)
    monkeypatch.setattr(tasks_repo, "get_task_tree", tree_spy)
    resp = (await world["client"].get("/api/v1/tasks", params={"scope": "mine", "q": "bakery", "limit": 6})).json()
    assert len(resp["tasks"]) == 6 and resp["next"] is None
    assert len(trees) == 6
    assert set(pages) == {router_mod._SEARCH_PAGE}
