"""Teams BFF — Issues.

Tier 2 mutating resource: ``CreateIssueBody`` and ``PatchIssueBody``
forbid extras, so unknown / smuggled fields are 422'd at the FastAPI
boundary. No adapter synthesis here — issues are pure user data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import AddIssueCommentBody, CreateIssueBody, PatchIssueBody

router = APIRouter()
_ctx = _agents._ctx


@router.get("/issues")
async def list_issues(ctx: TeamsContext = Depends(_ctx)):
    """List issues in the caller's company.

    Upstream ``GET /api/companies/{co}/issues`` returns a bare JSON
    array. The frontend ``IssuesPanel`` reads ``data?.issues`` (envelope
    shape) so without this wrap the list silently renders empty.
    """
    rows = await _agents._admin().list_issues(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
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
        body=body.model_dump(exclude_none=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/issues/{issue_id}")
async def patch_issue(
    issue_id: str,
    body: PatchIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch an issue."""
    return await _agents._admin().patch_issue(
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True),
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
        body=body.model_dump(),
        session_cookie=ctx.session_cookie,
    )
