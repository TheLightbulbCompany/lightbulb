"""`task_board` under the switch (wave-2 O Task 12; contracts-v2 §14.9): reads through `can_view`, changes as
signals to `TaskWorkflow` or a task-room note, never a row write; switch off ⇒ today's Paperclip path."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core import tasks_schema as ts
from core.auth import AuthContext
from core.repositories import tasks_repo as repo
from core.services import task_board

from .test_tasks_repo import table  # noqa: F401 -- moto tasks table fixture

WS = "org_1"
SAM, PRIYA = "user_sam", "user_priya"
FIN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUTH = AuthContext(user_id=SAM, org_id=WS)


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    w = {"on": True, "updates": [], "notes": [], "legacy": 0}

    async def update(ws, task_id, name, arg):
        w["updates"].append((task_id, name, arg))

    async def post(ws, room_id, **kw):
        w["notes"].append((room_id, kw["text"], kw.get("kind")))
        return "m1"

    async def ensure_room(ws, task, **kw):
        return ts.task_room_id(task["taskId"])

    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": FIN, "name": "Finance"}])
    )
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Sam"))
    monkeypatch.setattr("core.temporal.client.update_task", update)
    monkeypatch.setattr("core.services.task_conversation.post_as_lightbulb", post)
    monkeypatch.setattr("core.services.task_conversation.ensure_task_room", ensure_room)
    monkeypatch.setattr("core.repositories.messaging_repo.get_room", AsyncMock(return_value=None))
    return w


async def _task(user=SAM, status="working", **over):
    tid = ts.new_task_id()
    row = {
        "taskId": tid,
        "identifier": over.pop("identifier", f"LB-{tid[:4]}"),
        "title": "Pay the invoice",
        "description": "Pay Acme $40",
        "status": "queued",
        "audience": {"kind": "person", "userIds": [user]},
        "lane": "private",
        "ownerUserId": user,
        "triggerId": "chat:m1",
        "specialist": FIN,
        **over,
    }
    await repo.create_task(WS, row)
    if status != "queued":
        await repo.transition_task(WS, tid, to=status)
    return tid


@pytest.mark.asyncio
async def test_task_board_reads_tasks_when_on(world):
    mine = await _task(identifier="LB-7")
    theirs = await _task(user=PRIYA)
    done = await _task(status="resolved")
    found = await task_board.find_tasks(AUTH)
    assert [t["id"] for t in found["tasks"]] == [mine]  # open, visible: not Priya's, not the resolved one
    assert found["tasks"][0] == {
        "identifier": "LB-7",
        "id": mine,
        "title": "Pay the invoice",
        "status": "in_progress",
        "priority": None,
        "assignee": "Finance",
        "updatedAt": found["tasks"][0]["updatedAt"],
    }
    assert [t["id"] for t in (await task_board.find_tasks(AUTH, status="done"))["tasks"]] == [done]
    assert (await task_board.find_tasks(AUTH, query="acme"))["tasks"][0]["id"] == mine
    assert (await task_board.find_tasks(AUTH, query="zzz"))["tasks"] == []
    assert "note" in await task_board.find_tasks(AUTH, assignee="nobody")
    read = await task_board.read_task(AUTH, "LB-7")
    assert read["id"] == mine and read["description"] == "Pay Acme $40" and read["comments"] == []
    for hidden in (theirs, "LB-999", "../agents"):
        with pytest.raises(HTTPException) as exc:
            await task_board.read_task(AUTH, hidden)
        assert exc.value.status_code == 404
    assert world["legacy"] == 0


@pytest.mark.asyncio
async def test_task_board_update_sends_update_not_write(world):
    tid = await _task()
    before = await repo.get_task(WS, tid)
    out = await task_board.update_task(AUTH, tid, status="done", append_note="Paid it myself.")
    assert out["id"] == tid
    assert [(t, n, a.by_user_id) for t, n, a in world["updates"]] == [(tid, "resolve", SAM)]
    assert world["notes"] == [(ts.task_room_id(tid), "**Sam** noted: Paid it myself.", "note")]
    assert (await repo.get_task(WS, tid))["version"] == before["version"]  # no row write from here
    await task_board.update_task(AUTH, tid, status="cancelled")
    assert world["updates"][-1][1] == "stop" and world["updates"][-1][2].by_user_id == SAM
    for bad in ({"status": "in_progress"}, {"priority": "high"}):
        with pytest.raises(HTTPException) as exc:
            await task_board.update_task(AUTH, tid, **bad)
        assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await task_board.update_task(AUTH, await _task(user=PRIYA), status="done")
    assert exc.value.status_code == 404 and len(world["updates"]) == 2


SKILL_DIR = Path(__file__).resolve().parents[5] / "infra" / "hermes" / "skills-src" / "lightbulb"


def test_task_wording_is_super_agent_only():
    """§14.11 without touching switch-off agents: skills are fleet-wide, so the task handoff wording lives in
    references/tasks.md, which only the super-agent Worker's instructions name; the global text keeps Paperclip."""
    from core.temporal.activities import WORKER_INSTRUCTIONS

    tasks_md = (SKILL_DIR / "references" / "tasks.md").read_text()
    assert "ask Lightbulb to start a task" in tasks_md
    assert "references/tasks.md" in WORKER_INSTRUCTIONS
    assert "tasks.md" not in (SKILL_DIR / "SKILL.md").read_text()  # not in the routing table: never loaded globally
    for path in [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]:
        if path.name != "tasks.md":
            assert "ask Lightbulb to start" not in path.read_text(), path.name
    assert (
        "A handoff that needs work is a Paperclip task"
        in (SKILL_DIR / "references" / "platform-primitives.md").read_text()
    )


@pytest.mark.asyncio
async def test_a_task_the_caller_cannot_see_is_not_found(world):
    """Isolation (ported from the Paperclip-era test_task_board.py): a private task of another person, and another
    workspace's task id, read as 404 and never show in a list."""
    theirs = await _task(user=PRIYA)
    for task in (theirs, "LB-9999"):
        with pytest.raises(HTTPException) as exc:
            await task_board.read_task(AUTH, task)
        assert exc.value.status_code == 404
    other_ws = AuthContext(user_id=SAM, org_id="org_other")
    mine = await _task()
    with pytest.raises(HTTPException) as exc:
        await task_board.read_task(other_ws, mine)
    assert exc.value.status_code == 404
    assert theirs not in {t["id"] for t in (await task_board.find_tasks(AUTH))["tasks"]}
    with pytest.raises(HTTPException) as exc:
        await task_board.update_task(AUTH, theirs, status="done")
    assert exc.value.status_code == 404 and world["updates"] == []


@pytest.mark.asyncio
async def test_hosted_update_needs_the_write_scope(world, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from core.services import hosted_mcp

    scopes = []
    monkeypatch.setattr(hosted_mcp, "_caller", lambda scope=hosted_mcp.READ_SCOPE: scopes.append(scope) or AUTH)
    monkeypatch.setattr(hosted_mcp, "_net_caller", lambda scope=hosted_mcp.READ_SCOPE: scopes.append(scope) or AUTH)
    with pytest.raises(ToolError, match="404"):
        await hosted_mcp.update_task("LB-9999", status="done")
    assert scopes == [hosted_mcp.WRITE_SCOPE]
