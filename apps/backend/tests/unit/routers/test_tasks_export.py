"""GET /tasks/export: the caller's workspace tasks as NDJSON, read from `tasks_repo`, never Paperclip."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core import tasks_schema as ts
from core.auth import AuthContext, get_current_user
from core.repositories import messaging_repo, tasks_repo
from routers import tasks as router_mod
from tests.unit.super_agent.orchestration.test_tasks_repo import table  # noqa: F401

WS = "org_1"


def _client(role: str) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_sam", org_id=WS, org_role=role)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", AsyncMock(return_value="wsc"))

    monkeypatch.setattr(router_mod, "_EXPORT_PAGE", 1)  # forces paging through the index


async def _task(title: str) -> str:
    row, _ = await tasks_repo.create_task(
        WS,
        {
            "taskId": ts.new_task_id(),
            "identifier": "LB-1",
            "title": title,
            "status": "queued",
            "audience": {"kind": "person", "userIds": ["user_priya"]},
            "lane": "private",
            "ownerUserId": "user_priya",
            "triggerId": ts.trigger_id("chat", ts.new_task_id()),
        },
    )
    return row["taskId"]


async def test_export_streams_every_workspace_task_as_ndjson(world):
    a, b = await _task("Draft the memo"), await _task("Book the venue")
    output = {"id": "effect:1", "kind": "link", "title": "Venue", "ref": {"url": "https://x.test"}}
    await tasks_repo.add_task_output(WS, b, output)

    r = await _client("org:admin").get("/api/v1/tasks/export")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert {line["taskId"] for line in lines} == {a, b}  # someone else's tasks too: the owner exports all
    for line in lines:
        assert set(line) == {"taskId", "status", "title", "createdAt", "resolvedAt", "outputs"}
        assert line["status"] == "queued" and line["createdAt"] and line["resolvedAt"] is None
    assert next(line for line in lines if line["taskId"] == b)["outputs"][0]["title"] == "Venue"


async def test_export_is_owner_only(world):
    await _task("Draft the memo")
    r = await _client("org:member").get("/api/v1/tasks/export")
    assert r.status_code == 403
