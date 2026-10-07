"""Teams BFF — Issue-thread interactions.

The issue-thread interaction surface lets a board user resolve the prompts an
agent raises mid-run: confirmations (accept / reject) and
``ask_user_questions`` (respond / cancel).

SECURITY — board-only + per-user session. Upstream Paperclip gates the four
resolution routes (accept / reject / respond / cancel) as **board-only**
(agent actors are 403'd) and **company-scoped against the session actor**.
So these are proxied with the caller's per-user Paperclip session cookie
(``ctx.session_cookie``), NEVER an instance-admin cookie — an admin cookie
would bypass the upstream company scope and reach any tenant's interaction
(cross-tenant IDOR). ``ctx.session_cookie`` is the browser-held per-user
``.session_token`` bound to the Clerk-authenticated user (see
``core.services.paperclip_session_cookie``).

Spec §5: every mutating body is whitelisted with ``extra="forbid"``, so an
``adapterType`` (or any other non-whitelisted field) smuggled through an
interaction payload is rejected with 422 at the FastAPI boundary.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends


from . import agents as _agents
from ._guards import require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import (
    AcceptInteractionBody,
    CancelInteractionBody,
    RejectInteractionBody,
    RespondInteractionBody,
)

router = APIRouter()
_ctx = _agents._ctx
logger = logging.getLogger(__name__)


def _suggested_tasks(row: dict) -> list[dict]:
    """The suggest_tasks draft rows nested under ``payload.tasks``, if any."""
    payload = row.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return [task for task in payload["tasks"] if isinstance(task, dict)]
    return []


async def _enrich_interaction_actors(ctx: TeamsContext, rows: list) -> None:
    """Stamp ``*Email`` / ``*ClerkUserId`` onto human actor ids in place.

    Interaction rows carry Paperclip principal ids (``createdByUserId``,
    ``resolvedByUserId``, and suggest_tasks draft ``assigneeUserId``) that the
    frontend can neither display nor join against the Clerk-keyed ``/members``
    roster. Mirror the comments enrichment (``issues._enrich_comment_authors``):
    principal → Clerk id → email, each unique principal resolved at most once.
    Best-effort — an unmapped principal or failed Clerk lookup leaves that id
    unstamped rather than failing the request.
    """
    principal_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("createdByUserId", "resolvedByUserId"):
            value = row.get(field)
            if isinstance(value, str) and value:
                principal_ids.add(value)
        for task in _suggested_tasks(row):
            value = task.get("assigneeUserId")
            if isinstance(value, str) and value:
                principal_ids.add(value)
    if not principal_ids:
        return

    principal_to_clerk = await _agents.build_principal_to_clerk(ctx)

    identity_by_principal: dict[str, dict] = {}
    for principal_id in principal_ids:
        clerk_user_id = principal_to_clerk.get(principal_id)
        if not clerk_user_id:
            continue
        try:
            email = await _agents._resolve_user_email(clerk_user_id)
        except Exception:
            logger.exception(
                "interactions.list: clerk email lookup failed for clerk_user_id=%s (paperclip principal=%s)",
                clerk_user_id,
                principal_id,
            )
            continue
        if email:
            identity_by_principal[principal_id] = {
                "email": email,
                "clerkUserId": clerk_user_id,
            }

    def _stamp(target: dict, source_field: str, prefix: str) -> None:
        identity = identity_by_principal.get(target.get(source_field))
        if identity:
            target[f"{prefix}Email"] = identity["email"]
            target[f"{prefix}ClerkUserId"] = identity["clerkUserId"]

    for row in rows:
        if not isinstance(row, dict):
            continue
        _stamp(row, "createdByUserId", "createdBy")
        _stamp(row, "resolvedByUserId", "resolvedBy")
        for task in _suggested_tasks(row):
            _stamp(task, "assigneeUserId", "assignee")


@router.get("/issues/{issue_id}/interactions")
async def list_issue_interactions(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the thread interactions for an issue (the issue-detail thread),
    with human actor ids resolved to emails.

    Upstream ``GET /api/issues/{id}/interactions`` returns a bare JSON array;
    wrapped ``{interactions: [...]}`` like the sibling list surfaces. Rows pass
    through verbatim except for the ``_enrich_interaction_actors`` stamp.
    """
    rows = await _agents._admin().list_issue_interactions(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    enveloped = envelope_list(rows, "interactions")
    try:
        await _enrich_interaction_actors(ctx, enveloped["interactions"])
    except Exception:
        # Enrichment must never take down the thread — return upstream verbatim.
        logger.exception("interactions.list: actor enrichment failed; returning raw payload")
    return enveloped


@router.post("/issues/{issue_id}/interactions/{interaction_id}/accept")
async def accept_interaction(
    issue_id: str,
    interaction_id: str,
    body: AcceptInteractionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Accept a pending thread interaction.

    Body is whitelisted to the optional ``selected_client_keys`` /
    ``selected_option_ids``; an empty body accepts all/defaults.

    A spend door: upstream's accept route calls
    ``queueResolvedInteractionContinuationWakeup`` (and
    ``queueIssueAssignmentWakeup`` for a continuation issue), which dispatches an
    agent run server-side inside Paperclip. Gated BEFORE the proxy call — there
    is no enforcement point downstream of it.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().accept_interaction(
        issue_id=issue_id,
        interaction_id=interaction_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/interactions/{interaction_id}/reject")
async def reject_interaction(
    issue_id: str,
    interaction_id: str,
    body: RejectInteractionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Reject a pending thread interaction, optionally with a reason.

    A spend door, despite the name: upstream's reject route ALSO calls
    ``queueResolvedInteractionContinuationWakeup``. The agent is blocked waiting
    on an answer, and "the human said no" is an answer — it resumes the run and
    costs model tokens exactly like an accept. Gated for the same reason.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().reject_interaction(
        issue_id=issue_id,
        interaction_id=interaction_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/interactions/{interaction_id}/respond")
async def respond_interaction(
    issue_id: str,
    interaction_id: str,
    body: RespondInteractionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Respond to an ``ask_user_questions`` interaction with the user's answers.

    Responding resumes a fresh Friday (or the assigned agent) run
    server-side inside Paperclip, so this is a spend door. Gated BEFORE the
    proxy call reaches Paperclip -- there is no enforcement point downstream
    of this proxy.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().respond_interaction(
        issue_id=issue_id,
        interaction_id=interaction_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/issues/{issue_id}/interactions/{interaction_id}/cancel")
async def cancel_interaction(
    issue_id: str,
    interaction_id: str,
    body: CancelInteractionBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Cancel an ``ask_user_questions`` interaction, optionally with a reason.

    A spend door for the same reason as reject: upstream's cancel route calls
    ``queueResolvedInteractionContinuationWakeup`` too, resuming the waiting
    agent. All four resolution routes (accept/reject/respond/cancel) wake the
    agent; all four are gated.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().cancel_interaction(
        issue_id=issue_id,
        interaction_id=interaction_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )
