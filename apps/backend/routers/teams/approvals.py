"""Teams BFF — Approvals.

Spec §5 + audit §3 (indirect ``adapterType`` carrier): the approve /
reject body schema is whitelisted to ``note`` and ``reason`` only.
``ApproveApprovalBody`` and ``RejectApprovalBody`` both use
``extra="forbid"``, so any client trying to smuggle an
``adapterType`` (or any other non-whitelisted field) through the
approval payload returns 422 at the FastAPI boundary.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from core.services import handoff_analytics, marketplace_hire

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import (
    AddApprovalCommentBody,
    ApproveApprovalBody,
    RejectApprovalBody,
    RequestApprovalRevisionBody,
    ResubmitApprovalBody,
)

router = APIRouter()
_ctx = _agents._ctx

# Interpolated into the fixed upstream path, so it may not carry "/", "." or "?".
# Same constraint as the issue/run ids (#3119); defined here until that lands.
ApprovalId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


async def _require_company_approval(approval_id: str, ctx: TeamsContext) -> dict:
    """The approval, or 404 unless it belongs to the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company — so the session alone does not pin an
    approval to THIS workspace. Same fence as issues' ``_require_company_issue``.
    """
    approval = await _agents._admin().get_approval(approval_id=approval_id, session_cookie=ctx.session_cookie)
    if not isinstance(approval, dict) or approval.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


async def _company_approval(approval_id: ApprovalId, ctx: TeamsContext = Depends(_ctx)) -> str:
    """Route dependency form of ``_require_company_approval`` (404 outside this workspace)."""
    await _require_company_approval(approval_id, ctx)
    return approval_id


CompanyApprovalId = Annotated[str, Depends(_company_approval)]


@router.get("/approvals")
async def list_approvals(ctx: TeamsContext = Depends(_ctx)):
    """List pending approvals for the caller's company."""
    upstream = await _agents._admin().list_approvals(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "approvals")


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: CompanyApprovalId,
    body: ApproveApprovalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Approve a pending approval, optionally with a reviewer note.

    For a MARKETPLACE hire this is also where the agent's workspace gets
    installed. Paperclip's approve flips the pre-created agent from
    ``pending_approval`` to ``idle``; the EFS workspace, the ``openclaw.json``
    merge, and the operator-device pairing are Isol8's half and it knows
    nothing about them. Running it here keeps the whole thing on the approving
    user's own authenticated request — no privileged write path, no event
    plumbing, and the user learns immediately if the install failed.

    Every other approval type passes through untouched
    (``deploy_for_approval`` no-ops unless the payload carries the marketplace
    metadata).
    """
    approved = await _agents._admin().approve_approval(
        approval_id=approval_id,
        note=body.note,
        session_cookie=ctx.session_cookie,
    )
    handoff_analytics.approval_answered(
        workspace_id=ctx.owner_id, user_id=ctx.user_id, approval_id=approval_id, decision="approved"
    )
    await marketplace_hire.deploy_for_approval(approval=approved, owner_id=ctx.owner_id)
    return approved


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: CompanyApprovalId,
    body: RejectApprovalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Reject a pending approval. ``reason`` is required. A rejection is a handoff (D12)."""
    rejected = await _agents._admin().reject_approval(
        approval_id=approval_id,
        reason=body.reason,
        session_cookie=ctx.session_cookie,
    )
    handoff_analytics.approval_answered(
        workspace_id=ctx.owner_id, user_id=ctx.user_id, approval_id=approval_id, decision="rejected"
    )
    return rejected


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: CompanyApprovalId, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single approval by id (upstream ``GET /api/approvals/{id}``),
    fenced to the caller's company. Read by the approval detail panel."""
    return await _require_company_approval(approval_id, ctx)


@router.get("/approvals/{approval_id}/comments")
async def list_approval_comments(approval_id: CompanyApprovalId, ctx: TeamsContext = Depends(_ctx)):
    """Upstream ``GET /api/approvals/{id}/comments``."""
    return await _agents._admin().list_approval_comments(approval_id=approval_id, session_cookie=ctx.session_cookie)


@router.post("/approvals/{approval_id}/comments", status_code=201)
async def add_approval_comment(
    approval_id: CompanyApprovalId,
    body: AddApprovalCommentBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Upstream ``POST /api/approvals/{id}/comments``."""
    return await _agents._admin().add_approval_comment(
        approval_id=approval_id, body=body.body, session_cookie=ctx.session_cookie
    )


@router.post("/approvals/{approval_id}/request-revision")
async def request_approval_revision(
    approval_id: CompanyApprovalId,
    body: RequestApprovalRevisionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Upstream ``POST /api/approvals/{id}/request-revision`` (board only;
    Paperclip's own 403 reaches the caller via ``PaperclipApiError``)."""
    return await _agents._admin().request_approval_revision(
        approval_id=approval_id,
        decision_note=body.decision_note,
        session_cookie=ctx.session_cookie,
    )


@router.post("/approvals/{approval_id}/resubmit")
async def resubmit_approval(
    approval_id: CompanyApprovalId,
    body: ResubmitApprovalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Upstream ``POST /api/approvals/{id}/resubmit``, without a replacement payload."""
    return await _agents._admin().resubmit_approval(approval_id=approval_id, session_cookie=ctx.session_cookie)


@router.get("/approvals/{approval_id}/issues")
async def list_approval_issues(approval_id: CompanyApprovalId, ctx: TeamsContext = Depends(_ctx)):
    """Upstream ``GET /api/approvals/{id}/issues`` — the tasks linked to the approval."""
    return await _agents._admin().list_approval_issues(approval_id=approval_id, session_cookie=ctx.session_cookie)
