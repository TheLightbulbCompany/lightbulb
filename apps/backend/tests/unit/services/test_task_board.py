"""The MCP task-board tools act only on the caller's workspace company and never post a comment."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from mcp.server.mcpserver.exceptions import ToolError

from core.auth import AuthContext
from core.services import hosted_mcp, task_board
from routers.teams import agents as teams_agents
from routers.teams.deps import TeamsContext

AUTH = AuthContext(user_id="user_a", org_id="org_a")
CTX = TeamsContext(
    user_id="user_a", org_id="org_a", owner_id="org_a", company_id="co_a", paperclip_user_id="pc_a", session_cookie="c"
)
OURS = {
    "id": "i1",
    "identifier": "ISOA-1",
    "companyId": "co_a",
    "title": "Ship it",
    "status": "todo",
    "priority": "high",
    "assigneeAgentId": "ag1",
    "description": "Do the thing.",
    "blockedBy": [{"identifier": "ISOA-0", "title": "Before", "status": "todo"}],
}
THEIRS = {**OURS, "id": "i2", "identifier": "OTHR-1", "companyId": "co_b"}


@pytest.fixture
def admin(monkeypatch):
    fake = AsyncMock()
    fake.get_issue.side_effect = lambda issue_id, session_cookie: {"ISOA-1": OURS, "i1": OURS}.get(issue_id, THEIRS)
    fake.list_agents.return_value = [{"id": "ag1", "name": "Engineering"}]
    fake.list_issues.return_value = [OURS]
    fake.list_issue_comments.return_value = [{"authorAgentId": "ag1", "body": "on it", "createdAt": "2026-10-01"}]
    fake.patch_issue.side_effect = lambda issue_id, body, session_cookie: {**OURS, **body}
    monkeypatch.setattr(teams_agents, "_admin", lambda: fake)
    monkeypatch.setattr(task_board, "_context", AsyncMock(return_value=CTX))
    return fake


@pytest.mark.asyncio
async def test_another_workspaces_task_is_not_found(admin):
    for call in (task_board.read_task(AUTH, "OTHR-1"), task_board.update_task(AUTH, "OTHR-1", status="done")):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        await task_board.read_task(AUTH, "../agents")
    admin.patch_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_and_read_are_scoped_to_the_callers_company(admin):
    found = await task_board.find_tasks(AUTH, assignee="engineering")
    kwargs = admin.list_issues.await_args.kwargs
    assert kwargs["company_id"] == "co_a"
    assert kwargs["params"]["assigneeAgentId"] == "ag1"
    assert kwargs["params"]["status"] == "backlog,todo,in_progress,in_review,blocked"
    assert found["tasks"][0]["assignee"] == "Engineering"
    task = await task_board.read_task(AUTH, "ISOA-1")
    assert task["description"] == "Do the thing." and task["blockedBy"][0]["identifier"] == "ISOA-0"
    assert task["comments"] == [{"author": "Engineering", "createdAt": "2026-10-01", "body": "on it"}]


@pytest.mark.asyncio
async def test_update_appends_a_note_and_never_comments(admin):
    result = await task_board.update_task(AUTH, "ISOA-1", status="in_review", append_note="Waiting on review.")
    body = admin.patch_issue.await_args.kwargs["body"]
    assert body["status"] == "in_review"
    assert body["description"].startswith("Do the thing.\n\n---\n**Note ")
    assert body["description"].endswith(":** Waiting on review.")
    assert set(body) == {"status", "description"}
    admin.add_issue_comment.assert_not_called()
    assert result["status"] == "in_review"
    with pytest.raises(HTTPException):
        await task_board.update_task(AUTH, "ISOA-1", status="shipped")


@pytest.mark.asyncio
async def test_hosted_update_needs_the_write_scope_and_explains_refusals(admin, monkeypatch):
    scopes = []
    monkeypatch.setattr(hosted_mcp, "_caller", lambda scope=hosted_mcp.READ_SCOPE: scopes.append(scope) or AUTH)
    with pytest.raises(ToolError, match="404"):
        await hosted_mcp.update_task("OTHR-1", status="done")
    assert scopes == [hosted_mcp.WRITE_SCOPE]


@pytest.mark.asyncio
async def test_a_concurrent_description_edit_is_kept(admin):
    reads = iter([OURS])  # the edit lands between the first read and the re-read
    admin.get_issue.side_effect = lambda issue_id, session_cookie: next(
        reads, {**OURS, "description": "Do the thing. Edited."}
    )
    await task_board.update_task(AUTH, "ISOA-1", append_note="Mine.")
    body = admin.patch_issue.await_args.kwargs["body"]
    assert body["description"].startswith("Do the thing. Edited.\n\n---\n**Note ")
    admin.patch_issue.reset_mock()
    edits = iter(range(100))
    admin.get_issue.side_effect = lambda issue_id, session_cookie: {**OURS, "description": f"v{next(edits)}"}
    with pytest.raises(HTTPException) as exc:
        await task_board.update_task(AUTH, "ISOA-1", append_note="Mine.")
    assert exc.value.status_code == 409
    admin.patch_issue.assert_not_awaited()
