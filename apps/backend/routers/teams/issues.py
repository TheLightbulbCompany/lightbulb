"""Teams BFF — Issues.

Tier 2 mutating resource: ``CreateIssueBody`` and ``PatchIssueBody``
forbid extras, so unknown / smuggled fields are 422'd at the FastAPI
boundary. No adapter synthesis here — issues are pure user data.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import AddIssueCommentBody, CreateIssueBody, PatchIssueBody

router = APIRouter()
_ctx = _agents._ctx

# Paperclip ``projects.id`` is a Postgres ``uuid`` column, so a malformed
# ``project`` filter makes upstream throw a uuid-cast error → 500 (whose
# body leaks the internal company path). Validate the shape here so a bad
# value is a local 422 before any upstream call — mirrors ``inbox.py``.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


@router.get("/issues")
async def list_issues(
    ctx: TeamsContext = Depends(_ctx),
    q: Optional[str] = Query(default=None, max_length=200),
    status: Optional[str] = Query(default=None, max_length=120),
    project: Optional[str] = Query(default=None, pattern=_UUID_RE),
    assignee: Optional[str] = Query(default=None, max_length=80),
    sort_field: Optional[str] = Query(default=None, alias="sortField", pattern=r"^(updated)$"),
    sort_dir: Optional[str] = Query(default=None, alias="sortDir", pattern=r"^(asc|desc)$"),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    """List issues in the caller's company.

    Upstream ``GET /api/companies/{co}/issues`` returns a bare JSON
    array. The frontend Tasks panel reads ``data?.issues`` (envelope
    shape) so without this wrap the list silently renders empty.

    Optional query params are forwarded to upstream's already-capable
    issues-list route so the Tasks surface can drive server-side
    search (``q``), status filtering, per-status board columns
    (``status``), assignee/project filters, and sort. Mirrors the
    pass-through style of the sibling ``inbox.py`` listing.
    """
    upstream_params: dict[str, str] = {}
    if q is not None:
        upstream_params["q"] = q
    if status is not None:
        upstream_params["status"] = status
    if project is not None:
        upstream_params["projectId"] = project
    if assignee is not None:
        upstream_params["assigneeAgentId"] = assignee
    if sort_field is not None:
        upstream_params["sortField"] = sort_field
    if sort_dir is not None:
        upstream_params["sortDir"] = sort_dir
    if limit is not None:
        upstream_params["limit"] = str(limit)

    kwargs: dict[str, Any] = {
        "company_id": ctx.company_id,
        "session_cookie": ctx.session_cookie,
    }
    if upstream_params:
        kwargs["params"] = upstream_params

    rows = await _agents._admin().list_issues(**kwargs)
    return envelope_list(rows, "issues")


@router.get("/issues/labels")
async def list_labels(ctx: TeamsContext = Depends(_ctx)):
    """List company labels for the Inbox filter chip dropdown.

    Wraps upstream's bare-array response in ``{labels: [...]}`` envelope so
    the frontend's panel hooks can read ``data?.labels`` like every other
    Teams BFF resource. See ``deps.envelope_list`` for the wrapping pattern.

    Registered before ``/issues/{issue_id}`` so FastAPI's path matcher picks
    the static segment over the parameterized one.
    """
    upstream = await _agents._admin().list_company_labels(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "labels")


# NOTE: any new static `/issues/<word>` routes (e.g. /issues/labels) MUST be
# registered ABOVE this handler — FastAPI's matcher would otherwise treat
# the static word as a value for {issue_id} and 404 with a misleading shape.
@router.get("/issues/{issue_id}")
async def get_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single issue by id."""
    return await _agents._admin().get_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues")
async def create_issue(
    body: CreateIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create an issue."""
    return await _agents._admin().create_issue(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/issues/{issue_id}")
async def patch_issue(
    issue_id: str,
    body: PatchIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch an issue.

    Uses ``exclude_unset=True`` (not ``exclude_none``) so the frontend
    can send ``assignee_agent_id: null`` / ``project_id: null`` to
    unassign — Pydantic only emits keys the caller explicitly provided,
    including those set to ``null``. Dropping ``exclude_none`` was
    needed because the read-only assignee row in IssueProperties was
    replaced with a real picker that needs an "Unassigned" path.
    """
    return await _agents._admin().patch_issue(
        issue_id=issue_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/issues/{issue_id}")
async def delete_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete an issue.

    Maps to upstream ``DELETE /api/issues/{id}``. The admin client's
    ``_delete`` helper treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/archive")
async def archive_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Archive an issue from the inbox.

    Maps to upstream ``POST /api/issues/{id}/inbox-archive``. The Inbox
    UI fades the row out + shows an undo toast for ~8s.
    """
    return await _agents._admin().archive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/unarchive")
async def unarchive_issue(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Restore an archived issue back to the inbox.

    Maps to upstream ``DELETE /api/issues/{id}/inbox-archive``. Drives the
    undo-archive toast.
    """
    return await _agents._admin().unarchive_issue(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-read")
async def mark_issue_read(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Mark an issue as read for the signed-in user.

    Maps to upstream ``POST /api/issues/{id}/read``.
    """
    return await _agents._admin().mark_issue_read(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/mark-unread")
async def mark_issue_unread(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Mark an issue as unread for the signed-in user.

    Maps to upstream ``DELETE /api/issues/{id}/read``.
    """
    return await _agents._admin().mark_issue_unread(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/comments")
async def list_issue_comments(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List comments on an issue.

    Maps to upstream ``GET /api/issues/{id}/comments``.
    """
    return await _agents._admin().list_issue_comments(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/comments")
async def add_issue_comment(
    issue_id: str,
    body: AddIssueCommentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Post a comment on an issue. Body is whitelisted to ``{body: str}``."""
    return await _agents._admin().add_issue_comment(
        issue_id=issue_id,
        body=body.model_dump(by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/children")
async def list_issue_children(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List an issue's descendants (the Sub-issues section).

    Forwards upstream's ``descendantOf`` filter on the issues-list route —
    matches the upstream IssueDetail sub-issues section (all descendants,
    flat). Wrapped ``{issues: [...]}`` like the other list surfaces.
    """
    rows = await _agents._admin().list_issues(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params={"descendantOf": issue_id},
    )
    return envelope_list(rows, "issues")


@router.post("/issues/{issue_id}/children")
async def create_issue_child(
    issue_id: str,
    body: CreateIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a sub-issue under ``issue_id``.

    Reuses ``CreateIssueBody`` (the same whitelist as top-level create);
    routes to upstream's ``/children`` endpoint, which wires the
    parent/child relation server-side.
    """
    return await _agents._admin().create_child_issue(
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/approvals")
async def list_issue_approvals(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the approvals linked to an issue (the issue-detail Approvals section)."""
    rows = await _agents._admin().list_issue_approvals(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "approvals")


@router.get("/issues/{issue_id}/runs")
async def list_issue_runs(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the agent runs for an issue (the issue-detail Runs section)."""
    rows = await _agents._admin().list_issue_runs(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "runs")


@router.get("/issues/{issue_id}/documents")
async def list_issue_documents(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the documents attached to an issue (the issue-detail Documents section)."""
    rows = await _agents._admin().list_issue_documents(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "documents")
