"""Teams BFF — Routines + Goals + Projects.

Three resources with identical-shape CRUD; bundled into one router
file because they're almost-pure proxies to upstream Paperclip with
no security hot edges. Each mutating handler validates with a
``_Strict`` body schema (``extra="forbid"``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from . import agents as _agents
from ._guards import require_always_on_entitlement, require_trial_runway
from .deps import TeamsContext, envelope_list
from .schemas import (
    CreateGoalBody,
    CreateProjectBody,
    CreateRoutineBody,
    CreateRoutineTriggerBody,
    PatchGoalBody,
    PatchProjectBody,
    PatchRoutineBody,
)

router = APIRouter()
_ctx = _agents._ctx


# ---- Routines ---------------------------------------------------------


@router.get("/routines")
async def list_routines(ctx: TeamsContext = Depends(_ctx)):
    """List routines in the caller's company."""
    upstream = await _agents._admin().list_routines(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "routines")


@router.post("/routines")
async def create_routine(
    body: CreateRoutineBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a routine.

    Always-on entitlement gate: a routine is scheduled automation, which is what
    the subscription buys. A trial owner's container scales to zero on tab close
    and nothing wakes it for a scheduled dispatch, so a routine they create would
    silently never fire — see require_always_on_entitlement.
    """
    await require_always_on_entitlement(ctx.owner_id)
    return await _agents._admin().create_routine(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.post("/routines/{routine_id}/triggers")
async def create_routine_trigger(
    routine_id: str,
    body: CreateRoutineTriggerBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Attach a schedule trigger (cron) to a routine.

    The cron schedule lives on a separate trigger, not the routine row —
    the create dialog calls this after creating the routine itself.

    Gated separately from ``create_routine``: the trigger IS the schedule, so an
    ungated trigger endpoint would let a trial owner attach autonomy to a routine
    that already exists (e.g. one seeded before the gate, or created upstream).
    """
    await require_always_on_entitlement(ctx.owner_id)
    return await _agents._admin().create_routine_trigger(
        routine_id=routine_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/routines/{routine_id}")
async def patch_routine(
    routine_id: str,
    body: PatchRoutineBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch a routine."""
    return await _agents._admin().patch_routine(
        routine_id=routine_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a routine. Idempotent — upstream 404 is swallowed."""
    return await _agents._admin().delete_routine(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/routines/{routine_id}/run")
async def run_routine(
    routine_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    ctx: TeamsContext = Depends(_ctx),
):
    """Manually fire a routine.

    Body is forwarded to upstream as-is — upstream's ``runRoutineSchema``
    accepts optional ``reason`` / ``inputOverrides`` and is the source of
    truth for validation.

    Firing a routine dispatches a run immediately server-side inside
    Paperclip, so this is a Meta-spend front door. The free-tier $3/day
    cap is checked here, BEFORE the proxy call reaches Paperclip.
    """
    await require_trial_runway(ctx.owner_id)
    return await _agents._admin().run_routine(
        routine_id=routine_id,
        body=body,
        session_cookie=ctx.session_cookie,
    )


@router.get("/routines/{routine_id}/runs")
async def list_routine_runs(
    routine_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """List run history for a routine."""
    upstream = await _agents._admin().list_routine_runs(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "runs")


@router.get("/routines/{routine_id}")
async def get_routine(routine_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single routine by id (the routine detail page)."""
    return await _agents._admin().get_routine(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )


# ---- Goals ------------------------------------------------------------


@router.get("/goals")
async def list_goals(ctx: TeamsContext = Depends(_ctx)):
    """List goals in the caller's company."""
    upstream = await _agents._admin().list_goals(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "goals")


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single goal by id (the goal detail page).

    Maps to upstream ``GET /api/goals/{id}`` which returns the full Goal row.
    Upstream scopes access by the goal's ``companyId`` (``assertCompanyAccess``),
    so a goal from another company yields a 403/404 — never cross-tenant data.
    """
    return await _agents._admin().get_goal(
        goal_id=goal_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/goals")
async def create_goal(
    body: CreateGoalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a goal."""
    return await _agents._admin().create_goal(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/goals/{goal_id}")
async def patch_goal(
    goal_id: str,
    body: PatchGoalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch a goal."""
    return await _agents._admin().patch_goal(
        goal_id=goal_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a goal.

    Maps to upstream ``DELETE /api/goals/{id}``. The admin client's
    ``_delete`` helper treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_goal(
        goal_id=goal_id,
        session_cookie=ctx.session_cookie,
    )


# ---- Projects ---------------------------------------------------------


@router.get("/projects")
async def list_projects(ctx: TeamsContext = Depends(_ctx)):
    """List projects in the caller's company."""
    upstream = await _agents._admin().list_projects(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "projects")


@router.get("/projects/{project_id}")
async def get_project(project_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single project by id."""
    return await _agents._admin().get_project(
        project_id=project_id,
        session_cookie=ctx.session_cookie,
    )


@router.post("/projects")
async def create_project(
    body: CreateProjectBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a project."""
    return await _agents._admin().create_project(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.patch("/projects/{project_id}")
async def patch_project(
    project_id: str,
    body: PatchProjectBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch a project."""
    return await _agents._admin().patch_project(
        project_id=project_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )
