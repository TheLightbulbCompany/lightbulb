"""Question read routes (wave2-Q Task 4, read half): `GET /api/v1/needs-you`, `GET /api/v1/tasks/{id}/questions`.
Visibility through O1's `tasks_access.can_view`; the switch off is a 404 on both."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core.auth import AuthContext, get_current_user
from core.repositories import questions_repo, tasks_repo
from core.services import super_agent_flags
from routers import questions as router_mod

WS = "org_1"
SAM, ADA, PRIYA = "user_sam", "user_ada", "user_priya"


def _q(task_id, qid="q-1", **over):
    return {
        "taskId": task_id,
        "qid": qid,
        "kind": "approval",
        "subkind": "purchase",
        "title": "buy a domain for $12",
        "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "askedBy": {"kind": "worker", "attempt": 1},
        "answerers": [SAM],
        "answererRule": "requester",
        "baseAnswerers": [SAM],
    } | over


@pytest.fixture
def world(table, monkeypatch):
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(super_agent_flags, "flags_for", flag)
    monkeypatch.setattr(router_mod, "human_display_name", AsyncMock(side_effect=lambda u: u.split("_")[1].title()))
    who = {"user": SAM}
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=who["user"], org_id=WS)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return {"client": client, "who": who, "flag": flag}


async def _task(audience):
    from core import tasks_schema as ts

    row, _ = await tasks_repo.create_task(
        WS,
        {
            "taskId": ts.new_task_id(),
            "identifier": "LB-1",
            "title": "Register the domain",
            "status": "queued",
            "audience": audience,
            "lane": "private",
            "ownerUserId": SAM,
            "triggerId": ts.trigger_id("chat", "m1"),
        },
    )
    return row["taskId"]


async def test_needs_you_lists_only_mine(world):
    task = await _task({"kind": "person", "userIds": [SAM]})
    await questions_repo.open_question(WS, _q(task))
    await questions_repo.open_question(WS, _q(task, "q-2", answerers=[ADA], baseAnswerers=[ADA]))
    resp = await world["client"].get("/api/v1/needs-you")
    assert resp.status_code == 200
    [item] = resp.json()["items"]
    assert (item["id"], item["taskId"], item["state"], item["canAnswer"]) == ("q-1", task, "open", True)
    assert item["waitingLabel"] == "Waiting on you"
    assert [o["style"] for o in item["options"]] == ["primary", "danger"]
    assert resp.json()["next"] is None


async def test_needs_you_drops_answered(world):
    task = await _task({"kind": "person", "userIds": [SAM]})
    await questions_repo.open_question(WS, _q(task))
    await questions_repo.record_answer(
        WS, task, "q-1", option_id="deny", trigger_id=None, by=SAM, client_answer_id="c", expected_version=None
    )
    assert (await world["client"].get("/api/v1/needs-you")).json()["items"] == []


async def test_history_shows_deny_and_cancel_read_only_for_viewers(world, monkeypatch):
    task = await _task({"kind": "workspace"})
    monkeypatch.setattr(router_mod.tasks_access, "_is_workspace_member", AsyncMock(return_value=True))
    await questions_repo.open_question(WS, _q(task, "q-1"))
    await questions_repo.open_question(WS, _q(task, "q-2"))
    await questions_repo.record_answer(
        WS, task, "q-1", option_id="deny", trigger_id=None, by=SAM, client_answer_id="c", expected_version=None
    )
    world["who"]["user"] = PRIYA  # a workspace member, not an answerer
    resp = await world["client"].get(f"/api/v1/tasks/{task}/questions")
    assert resp.status_code == 200
    items = {i["id"]: i for i in resp.json()["items"]}
    assert items["q-1"]["state"] == "answered" and items["q-1"]["answer"]["optionId"] == "deny"
    assert items["q-1"]["answeredBy"] == {"id": SAM, "name": "Sam"} and items["q-1"]["waitingLabel"] is None
    assert items["q-2"]["canAnswer"] is False and items["q-2"]["waitingLabel"] == "Waiting on Sam"
    await questions_repo.cancel_open(WS, task, by=PRIYA, reason="stopped")
    q2 = {i["id"]: i for i in (await world["client"].get(f"/api/v1/tasks/{task}/questions")).json()["items"]}["q-2"]
    assert (q2["state"], q2["reason"], q2["cancelledBy"]["id"]) == ("cancelled", "stopped", PRIYA)


async def test_private_task_404_for_non_viewer(world):
    task = await _task({"kind": "person", "userIds": [SAM]})
    await questions_repo.open_question(WS, _q(task))
    world["who"]["user"] = PRIYA
    resp = await world["client"].get(f"/api/v1/tasks/{task}/questions")
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found"


async def test_unknown_task_404(world):
    resp = await world["client"].get("/api/v1/tasks/8f14e45f-ceea-467a-9575-8a1b2c3d4e5f/questions")
    assert resp.status_code == 404


async def test_switch_off_404(world, monkeypatch):
    world["flag"].return_value = False
    spy = AsyncMock(side_effect=AssertionError("tasks table read with the switch off"))
    monkeypatch.setattr(questions_repo, "needs_for_user", spy)
    assert (await world["client"].get("/api/v1/needs-you")).status_code == 404
    assert (await world["client"].get("/api/v1/tasks/x/questions")).status_code == 404
    spy.assert_not_awaited()
