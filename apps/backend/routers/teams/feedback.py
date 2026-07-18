"""Teams BFF — agent feedback votes (thumbs up/down on agent output).

Paperclip's feedback-votes pipeline is the ONLY reinforcement signal back
into agents: a board user thumbs-up/down an agent comment (or document
revision) and that vote is fed back to the agent. This surface exposes the
cast/list vote proxy plus a consent-gated feedback-traces listing.

Tier 2 mutating resource: ``UpsertFeedbackVoteBody`` forbids extras, so a
smuggled ``adapterType`` / ``adapterConfig`` is 422'd at the FastAPI boundary
(same posture as ``issues.py``). No adapter synthesis here.

Consent gate — feedback traces are the shared-with-Labs export records. They
only exist once a company opts into ``feedbackDataSharingEnabled``. Both the
read AND write paths honor it: the trace listing returns an empty list WITHOUT
calling upstream when sharing is disabled, and ``cast_feedback_vote`` forces
``allowSharing`` false before forwarding a vote so a direct POST cannot create
a shared trace for a disabled-sharing company. Fail closed on both.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import UpsertFeedbackVoteBody

router = APIRouter()
_ctx = _agents._ctx
logger = logging.getLogger(__name__)


@router.get("/issues/{issue_id}/feedback-votes")
async def list_feedback_votes(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the caller's feedback votes on an issue's agent outputs.

    Upstream ``GET /api/issues/{id}/feedback-votes`` returns a bare array;
    wrapped ``{votes: [...]}`` like the other Teams list surfaces so the
    frontend reads ``data?.votes``. The UI keys these by ``targetId`` to render
    the active thumbs-up/down state per agent comment.
    """
    rows = await _agents._admin().list_issue_feedback_votes(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "votes")


@router.post("/issues/{issue_id}/feedback-votes")
async def cast_feedback_vote(
    issue_id: str,
    body: UpsertFeedbackVoteBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Cast (upsert) a thumbs-up/down vote on an agent output.

    Body is whitelisted to ``{targetType, targetId, vote, reason?,
    allowSharing?}`` — no adapter smuggling. Unlike an issue comment, a vote
    does NOT trigger an agent run server-side, so there is no spend front door
    to gate here. Forwards to upstream ``POST /api/issues/{id}/feedback-votes``.

    CONSENT-GATED write: ``allowSharing:true`` creates a shared-with-Labs trace
    payload, so it may only be forwarded once the company opts into
    ``feedbackDataSharingEnabled`` — the SAME gate ``list_feedback_traces``
    applies to the read path. A direct POST cannot share for a company that has
    sharing disabled: we read the consent flag and force ``allowSharing`` false
    when it is off. Fail closed — a missing/non-dict company is treated as
    disabled.
    """
    admin = _agents._admin()
    forwarded = body.model_dump(exclude_none=True, by_alias=True)

    if forwarded.get("allowSharing"):
        company = await admin.get_company(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        )
        sharing_enabled = bool(isinstance(company, dict) and company.get("feedbackDataSharingEnabled"))
        if not sharing_enabled:
            forwarded["allowSharing"] = False

    return await admin.upsert_issue_feedback_vote(
        issue_id=issue_id,
        body=forwarded,
        session_cookie=ctx.session_cookie,
    )


@router.get("/issues/{issue_id}/feedback-traces")
async def list_feedback_traces(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List an issue's feedback traces — CONSENT-GATED.

    Traces are the shared-with-Labs export records for votes on this issue.
    The company's ``feedbackDataSharingEnabled`` flag is the consent gate: if
    sharing is disabled we return an empty ``{traces: []}`` WITHOUT calling
    upstream, so a disabled-sharing company never leaks trace payloads. When
    sharing is enabled we proxy the COMPANY-scoped
    ``GET /api/companies/{companyId}/feedback-traces`` (with ``issueId`` as a
    query filter — there is no issue-scoped traces route upstream) and envelope
    the bare array.
    """
    admin = _agents._admin()
    company = await admin.get_company(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    sharing_enabled = bool(isinstance(company, dict) and company.get("feedbackDataSharingEnabled"))
    if not sharing_enabled:
        return {"traces": []}

    rows = await admin.list_issue_feedback_traces(
        company_id=ctx.company_id,
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "traces")
