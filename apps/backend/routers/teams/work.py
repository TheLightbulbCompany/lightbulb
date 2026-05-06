"""Teams BFF — Routines + Goals + Projects.

Three resources with identical-shape CRUD; bundled into one router
file because they're almost-pure proxies to upstream Paperclip with
no security hot edges. Each mutating handler validates with a
``_Strict`` body schema (``extra="forbid"``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from . import agents as _agents
from .deps import TeamsContext, envelope_list
from .schemas import (
    CreateGoalBody,
    CreateProjectBody,
    CreateRoutineBody,
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
    """Create a routine."""
    return await _agents._admin().create_routine(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True),
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
        body=body.model_dump(exclude_none=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a routine. Idempotent — upstream 404 is swallowed."""
    return await _agents._admin().delete_routine(
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


@router.post("/goals")
async def create_goal(
    body: CreateGoalBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a goal."""
    return await _agents._admin().create_goal(
        company_id=ctx.company_id,
        body=body.model_dump(exclude_none=True),
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
        body=body.model_dump(exclude_none=True),
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
        body=body.model_dump(exclude_none=True),
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
        body=body.model_dump(exclude_none=True),
        session_cookie=ctx.session_cookie,
    )
