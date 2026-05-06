"""Teams BFF — Approvals.

Spec §5 + audit §3 (indirect ``adapterType`` carrier): the approve /
reject body schema is whitelisted to ``note`` and ``reason`` only.
``ApproveApprovalBody`` and ``RejectApprovalBody`` both use
``extra="forbid"``, so any client trying to smuggle an
``adapterType`` (or any other non-whitelisted field) through the
approval payload returns 422 at the FastAPI boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import ApproveApprovalBody, RejectApprovalBody

router = APIRouter()
_ctx = _agents._ctx


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
    approval_id: str,
    body: ApproveApprovalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Approve a pending approval, optionally with a reviewer note."""
    return await _agents._admin().approve_approval(
        approval_id=approval_id,
        note=body.note,
        session_cookie=ctx.session_cookie,
    )


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: str,
    body: RejectApprovalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Reject a pending approval. ``reason`` is required."""
    return await _agents._admin().reject_approval(
        approval_id=approval_id,
        reason=body.reason,
        session_cookie=ctx.session_cookie,
    )


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single approval by id.

    Maps to upstream ``GET /api/approvals/{id}``. Used by the approval
    detail page that Inbox approval rows link into.
    """
    return await _agents._admin().get_approval(
        approval_id=approval_id,
        session_cookie=ctx.session_cookie,
    )
