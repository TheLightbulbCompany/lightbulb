"""Teams BFF — issue tree-control (operator safety lever).

Surfaces Paperclip's issue tree-holds: freeze / resume / cancel an entire
issue subtree, plus the read-only subtree/blocker diagnostics that complement
pause/resume. Mirrors the ``issues.py`` proxy pattern — ``TeamsContext`` auth,
whitelisted ``_Strict`` bodies, ``envelope_list`` for the bare-array list.

Upstream authz: the tree-hold / tree-control routes are board-only
(``assertBoard``); the two diagnostics routes (subtree / blockers) instead use
issue-read authz (``assertIssueReadAllowed``, in upstream ``issues.ts``) —
still company-scoped and read-only, just not board-only.

Spend note: these are STOP levers (pause/cancel interrupt active runs and
defer wakeups upstream), so they are deliberately NOT gated by
``require_trial_runway`` — gating a "stop the runaway" button behind the
free-tier cap would be perverse (you could not stop spend precisely when you
are over budget). The one upstream mode that SPAWNS spend — ``restore`` with
``metadata.wakeAgents`` (``heartbeat.wakeup`` agent runs, forwarded ungated) —
is deliberately not exposed: the BFF ``mode`` whitelist omits ``restore`` and
``CreateTreeHoldBody`` drops the free-form ``metadata`` field (see
``schemas.py``). Any residual inference stays bounded per-request by the
bedrock-gate.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import CreateTreeHoldBody, PreviewTreeControlBody, ReleaseTreeHoldBody

router = APIRouter()
_ctx = _agents._ctx

# Upstream hold ids are Postgres ``uuid`` columns; a malformed value makes
# upstream throw a uuid-cast 500 (whose body can leak the internal company
# path). Validate the shape here so a bad id is a local 400 — mirrors
# upstream's own ``isUuidLike`` guard on the hold routes.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


@router.post("/issues/{issue_id}/tree-control/preview")
async def preview_tree_control(
    issue_id: str,
    body: PreviewTreeControlBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Preview the blast radius of a subtree control action.

    Maps to upstream ``POST /api/issues/{id}/tree-control/preview``. Read-only
    — applies nothing; returns affected-issue / active-run totals + warnings so
    the operator can see the impact before freezing/cancelling.
    """
    return await _agents._admin().preview_issue_tree_control(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        body=body.model_dump(by_alias=True, exclude_none=True),
    )


@router.post("/issues/{issue_id}/tree-holds")
async def create_tree_hold(
    issue_id: str,
    body: CreateTreeHoldBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a subtree hold (pause / resume / cancel).

    Maps to upstream ``POST /api/issues/{id}/tree-holds``. For pause/cancel,
    upstream interrupts the subtree's active runs and defers unclaimed wakeups;
    returns ``{hold, preview}``.
    """
    return await _agents._admin().create_issue_tree_hold(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        body=body.model_dump(by_alias=True, exclude_none=True),
    )


@router.get("/issues/{issue_id}/tree-holds")
async def list_tree_holds(
    issue_id: str,
    ctx: TeamsContext = Depends(_ctx),
    status: Optional[str] = Query(default=None, pattern=r"^(active|released)$"),
    mode: Optional[str] = Query(default=None, pattern=r"^(pause|resume|cancel|restore)$"),
    include_members: Optional[bool] = Query(default=None, alias="includeMembers"),
):
    """List the subtree holds rooted at an issue.

    Maps to upstream ``GET /api/issues/{id}/tree-holds`` (bare array). Optional
    ``status`` / ``mode`` / ``includeMembers`` filters forward to upstream.
    Wrapped ``{holds: [...]}`` like the other Teams list surfaces.
    """
    params: dict[str, str] = {}
    if status is not None:
        params["status"] = status
    if mode is not None:
        params["mode"] = mode
    if include_members:
        params["includeMembers"] = "true"

    rows = await _agents._admin().list_issue_tree_holds(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        params=params or None,
    )
    return envelope_list(rows, "holds")


@router.get("/issues/{issue_id}/tree-holds/{hold_id}")
async def get_tree_hold(
    issue_id: str,
    hold_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Fetch a single subtree hold by id.

    Maps to upstream ``GET /api/issues/{id}/tree-holds/{holdId}``.
    """
    if not _is_uuid(hold_id):
        raise HTTPException(status_code=400, detail="Invalid hold ID")
    return await _agents._admin().get_issue_tree_hold(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        hold_id=hold_id,
    )


@router.post("/issues/{issue_id}/tree-holds/{hold_id}/release")
async def release_tree_hold(
    issue_id: str,
    hold_id: str,
    body: ReleaseTreeHoldBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Release an active subtree hold.

    Maps to upstream ``POST /api/issues/{id}/tree-holds/{holdId}/release``.
    """
    if not _is_uuid(hold_id):
        raise HTTPException(status_code=400, detail="Invalid hold ID")
    return await _agents._admin().release_issue_tree_hold(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
        hold_id=hold_id,
        body=body.model_dump(by_alias=True, exclude_none=True),
    )


@router.get("/issues/{issue_id}/tree-control/state")
async def get_tree_control_state(
    issue_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Fetch the active-pause-hold gate for an issue.

    Maps to upstream ``GET /api/issues/{id}/tree-control/state``. Returns
    ``{activePauseHold: {...}|null}`` — non-null (with ``isRoot``) when this
    issue is under an active subtree pause hold. Drives the UI's
    pause/resume affordance.
    """
    return await _agents._admin().get_issue_tree_control_state(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )


@router.get("/issues/{issue_id}/diagnostics/subtree")
async def get_subtree_diagnostics(
    issue_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Read the subtree diagnostics for an issue.

    Maps to upstream ``GET /api/issues/{id}/diagnostics/subtree`` — read-only
    node/edge/readiness view that complements the pause/resume lever.
    """
    return await _agents._admin().get_issue_subtree_diagnostics(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )


@router.get("/issues/{issue_id}/diagnostics/blockers")
async def get_blocker_diagnostics(
    issue_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Read the blocker diagnostics for an issue.

    Maps to upstream ``GET /api/issues/{id}/diagnostics/blockers`` — why an
    issue is (or is not) blocked, complementing the subtree control view.
    """
    return await _agents._admin().get_issue_blocker_diagnostics(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )


def _is_uuid(value: str) -> bool:
    return bool(re.match(_UUID_RE, value))
