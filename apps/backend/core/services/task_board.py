"""The workspace task board (Paperclip issues) for the Lightbulb MCP tools.

Both MCP surfaces call these with an AuthContext: the hosted MCP (OAuth person) and the
paired-Mac device routes (routers/messaging.py, /local-sessions/device/*). They go through
the Teams BFF's own context, company fence and PATCH handler, so a tool can do exactly what
the person's Teams panel can, in the workspace the caller is signed in to, and nothing else.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import HTTPException

from core.auth import AuthContext
from core.services.paperclip_admin_client import ISSUE_ID_PATTERN

OPEN_STATUSES = ("backlog", "todo", "in_progress", "in_review", "blocked")
STATUSES = (*OPEN_STATUSES, "done", "cancelled")
PRIORITIES = ("critical", "high", "medium", "low")
NOTE_MAX = 4000
APPEND_ATTEMPTS = 3


class _NoCookieJar:
    """No browser here: a minted Paperclip session stays in the in-process backstop."""

    def set_cookie(self, *args, **kwargs) -> None:
        return None


async def _context(auth: AuthContext):
    """The caller's Teams context: workspace company, membership and Paperclip session.
    No self-heal: a person not yet joined gets the Teams 409/202, not a provision."""
    from routers.teams import agents as teams_agents
    from routers.teams import deps as teams_deps

    return await teams_deps.resolve_teams_context(
        auth=auth,
        repo=teams_agents._repo(),
        session_factory=teams_agents.get_user_session_cookie_for,
        cookies={},
        response=_NoCookieJar(),
    )


async def _agent_names(ctx) -> dict[str, str]:
    from routers.teams import agents as teams_agents
    from routers.teams.deps import envelope_list

    listed = await teams_agents._admin().list_agents(company_id=ctx.company_id, session_cookie=ctx.session_cookie)
    return {a["id"]: a.get("name") or "" for a in envelope_list(listed, "agents")["agents"] if isinstance(a, dict)}


async def _issue(task: str, ctx) -> dict:
    """The task by identifier (ISOA-123) or id, only if it is in this workspace's company."""
    from routers.teams import issues as teams_issues

    if not isinstance(task, str) or not re.match(ISSUE_ID_PATTERN, task) or len(task) > 64:
        raise HTTPException(status_code=404, detail="Task not found")
    return await teams_issues._require_company_issue(task, ctx)


def _summary(issue: dict, names: dict[str, str]) -> dict:
    agent = issue.get("assigneeAgentId")
    return {
        "identifier": issue.get("identifier"),
        "id": issue.get("id"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": names.get(agent, "an agent") if agent else ("a person" if issue.get("assigneeUserId") else None),
        "updatedAt": issue.get("updatedAt"),
    }


async def find_tasks(auth: AuthContext, query: str = "", status: str = "", assignee: str = "", limit: int = 25) -> dict:
    from routers.teams import issues as teams_issues

    ctx = await _context(auth)
    names = await _agent_names(ctx)
    wanted = [s.strip() for s in status.split(",") if s.strip()] if status else list(OPEN_STATUSES)
    if any(s not in STATUSES for s in wanted):
        raise HTTPException(status_code=422, detail=f"status must be one or more of {', '.join(STATUSES)}")
    agent_id = None
    if assignee.strip():
        needle = assignee.casefold().strip()
        exact = [i for i, n in names.items() if n.casefold() == needle]
        matches = exact or [i for i, n in names.items() if needle in n.casefold()]
        if len(matches) != 1:
            return {"tasks": [], "note": f"{len(matches)} agents match assignee {assignee!r}; use an exact agent name."}
        agent_id = matches[0]
    listed = await teams_issues.list_issues(
        ctx=ctx,
        q=query.strip()[:200] or None,
        status=",".join(wanted),
        project=None,
        assignee=agent_id,
        participant_agent_id=None,
        sort_field="updated",
        sort_dir="desc",
        limit=max(1, min(limit, 100)),
        exclude_routine_executions=True,
    )
    return {"tasks": [_summary(i, names) for i in listed["issues"] if isinstance(i, dict)]}


async def read_task(auth: AuthContext, task: str, comments: int = 10) -> dict:
    from routers.teams import issues as teams_issues

    ctx = await _context(auth)
    issue = await _issue(task, ctx)
    names = await _agent_names(ctx)
    thread = await teams_issues.list_issue_comments(issue["id"], ctx)
    rows = [c for c in (thread if isinstance(thread, list) else thread.get("comments") or []) if isinstance(c, dict)]
    rows.sort(key=lambda c: c.get("createdAt") or "")
    return {
        **_summary(issue, names),
        "description": issue.get("description") or "",
        "blockedBy": [
            {"identifier": b.get("identifier"), "title": b.get("title"), "status": b.get("status")}
            for b in issue.get("blockedBy") or []
            if isinstance(b, dict)
        ],
        "comments": [
            {
                "author": names.get(c.get("authorAgentId"), "an agent")
                if c.get("authorAgentId")
                else c.get("authorName") or "a person",
                "createdAt": c.get("createdAt"),
                "body": c.get("body"),
            }
            for c in rows[-max(0, min(comments, 50)) :]
        ]
        if comments > 0
        else [],
    }


async def update_task(
    auth: AuthContext, task: str, status: str | None = None, priority: str | None = None, append_note: str | None = None
) -> dict:
    """Status/priority change and a note appended to the description, through the Teams PATCH.
    Never a comment: a Paperclip comment wakes the assigned agent and spends."""
    from routers.teams import issues as teams_issues
    from routers.teams.schemas import PatchIssueBody

    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")
    if priority is not None and priority not in PRIORITIES:
        raise HTTPException(status_code=422, detail=f"priority must be one of {', '.join(PRIORITIES)}")
    note = (append_note or "").strip()
    if len(note) > NOTE_MAX:
        raise HTTPException(status_code=422, detail=f"append_note is longer than {NOTE_MAX} characters")
    if status is None and priority is None and not note:
        raise HTTPException(status_code=422, detail="Nothing to change: give status, priority or append_note.")
    ctx = await _context(auth)
    names = await _agent_names(ctx)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Paperclip's PATCH has no precondition (no If-Match), so an appended note re-reads the
    # description just before writing and rebuilds on a change, rather than overwriting an edit.
    # ponytail: a write landing between the re-read and the PATCH is still lost; a precondition
    # upstream would close it.
    for _ in range(APPEND_ATTEMPTS):
        issue = await _issue(task, ctx)
        changes: dict = {}
        if status is not None:
            changes["status"] = status
        if priority is not None:
            changes["priority"] = priority
        if note:
            current = (issue.get("description") or "").rstrip()
            changes["description"] = f"{current}\n\n---\n**Note {stamp}:** {note}".lstrip()
        try:
            body = PatchIssueBody(**changes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="The description would be too long to save.") from exc
        if note and (await _issue(task, ctx)).get("description") != issue.get("description"):
            continue
        updated = await teams_issues.patch_issue(issue["id"], body, ctx)
        return _summary(updated if isinstance(updated, dict) and updated.get("id") else {**issue, **changes}, names)
    raise HTTPException(
        status_code=409, detail="The task's description kept changing while the note was added. Nothing was saved."
    )
