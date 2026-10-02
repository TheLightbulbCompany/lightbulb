"""Teams BFF — decisions (agent-proposed choices a person answers).

An agent that hits an authority edge files a decision: a title, a body, one to
eight options and up to four inputs. Each option carries effects Paperclip runs
itself when a person picks it (comment, set status, reassign, clear a blocker,
create a child issue, cancel an issue tree), and the agent is then woken with
the outcome. Open decisions already reach the attention feed (``attention.py``,
``sourceKind: "decision"``) but the feed row carries no options; these routes
are what make one answerable from the Inbox.

SECURITY — board-only + per-user session. Upstream gates decide/dismiss with
``assertBoard`` and runs the chosen option's effects AS the deciding user, so
every call here carries the caller's own Paperclip session cookie
(``ctx.session_cookie``), NEVER the instance-admin cookie. Bodies are strict
(``_Strict``), so nothing but the option id, the input answers and the
idempotency key is forwarded.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from . import agents as _agents
from ._guards import require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import DecideDecisionBody, DismissDecisionBody

router = APIRouter()
_ctx = _agents._ctx

# Interpolated into the fixed upstream path, so it may not carry "/", "." or "?".
DecisionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


async def _company_decision(decision_id: DecisionId, ctx: TeamsContext = Depends(_ctx)) -> dict:
    """The decision, or 404 unless it belongs to the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company — so the session alone does not pin a
    decision to THIS workspace. Same fence as approvals' ``_require_company_approval``.
    """
    decision = await _agents._admin().get_decision(decision_id=decision_id, session_cookie=ctx.session_cookie)
    if not isinstance(decision, dict) or decision.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail="Decision not found")
    return decision


CompanyDecision = Annotated[dict, Depends(_company_decision)]


@router.get("/decisions")
async def list_decisions(
    ctx: TeamsContext = Depends(_ctx),
    status: str = Query(default="open", pattern=r"^(open|decided|expired|cancelled)$"),
    # Paperclip's own cap; it defaults to 50 when omitted.
    limit: Optional[int] = Query(default=None, ge=1, le=100),
):
    """The caller's company's decisions in one status, newest first.

    Upstream ``GET /api/companies/{companyId}/decisions`` returns a bare array;
    wrapped ``{decisions: [...]}`` like the other Teams list surfaces. Each open
    row carries ``targetChanged`` (which target issues drifted since it was
    proposed) — the detail read does not, so every decision card on screen takes
    it from this ONE shared read instead of a list call per card.
    """
    rows = await _agents._admin().list_decisions(
        company_id=ctx.company_id,
        status=status,
        limit=limit,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "decisions")


@router.get("/decisions/{decision_id}")
async def get_decision(decision: CompanyDecision):
    """One decision plus its ``executions`` (upstream ``GET /api/decisions/{id}``),
    fenced to the caller's company."""
    return decision


@router.post("/decisions/{decision_id}/decide")
async def decide_decision(
    decision: CompanyDecision,
    body: DecideDecisionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Pick an option. Paperclip runs its effects as the caller and wakes the
    proposing agent with the outcome — a spend door, so it is gated on trial
    runway before the proxy call like the interaction-resolution routes.

    Upstream's refusals pass through the ``PaperclipApiError`` handler with
    their own status: 403 (signature verification failed), 409 (already
    resolved / expired), 422 (unknown option, missing required input).
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().decide_decision(
        decision_id=decision["id"],
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/decisions/{decision_id}/dismiss")
async def dismiss_decision(
    decision: CompanyDecision,
    body: DismissDecisionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Dismiss without running any effect. "The human said no" is still an
    answer the proposing agent is woken with, so it is gated like decide."""
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().dismiss_decision(
        decision_id=decision["id"],
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )
