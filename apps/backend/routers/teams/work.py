"""Teams BFF — Routines + Goals + Projects.

Three resources with identical-shape CRUD; bundled into one router
file because they're almost-pure proxies to upstream Paperclip with
no security hot edges. Each mutating handler validates with a
``_Strict`` body schema (``extra="forbid"``).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from core.services.trigger_fire_url import stamp_fire_urls

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
    UpdateRoutineTriggerBody,
)

router = APIRouter()
_ctx = _agents._ctx

# Interpolated into a fixed upstream path, so it may not carry "/", "." or "?".
RoutineId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]


def _same_company(entity: Any, ctx: TeamsContext, what: str) -> dict:
    """404 unless ``entity`` is in the caller's workspace company.

    Paperclip authorizes by the member's session, and a Paperclip user can hold
    memberships in more than one company, so the session alone does not pin an
    entity to THIS workspace. Same fence as issues' ``CompanyIssueId``.
    """
    if not isinstance(entity, dict) or entity.get("companyId") != ctx.company_id:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return entity


async def _company_goal(goal_id: RoutineId, ctx: TeamsContext = Depends(_ctx)) -> str:
    _same_company(await _agents._admin().get_goal(session_cookie=ctx.session_cookie, goal_id=goal_id), ctx, "Goal")
    return goal_id


async def _company_project(project_id: RoutineId, ctx: TeamsContext = Depends(_ctx)) -> str:
    project = await _agents._admin().get_project(session_cookie=ctx.session_cookie, project_id=project_id)
    _same_company(project, ctx, "Project")
    return project_id


async def _company_routine(routine_id: RoutineId, ctx: TeamsContext = Depends(_ctx)) -> str:
    routine = await _agents._admin().get_routine(session_cookie=ctx.session_cookie, routine_id=routine_id)
    _same_company(routine, ctx, "Routine")
    return routine_id


async def _company_routine_trigger(
    routine_id: RoutineId, trigger_id: RoutineId, ctx: TeamsContext = Depends(_ctx)
) -> str:
    """The routine is in this workspace AND the trigger is one of its triggers.

    Upstream's ``/api/routine-triggers/{id}`` (patch, delete, rotate-secret) is
    NOT routine-scoped, so fencing the routine alone would not pin the trigger.
    """
    routine = await _agents._admin().get_routine(session_cookie=ctx.session_cookie, routine_id=routine_id)
    _same_company(routine, ctx, "Routine")
    if not any(isinstance(t, dict) and t.get("id") == trigger_id for t in routine.get("triggers") or []):
        raise HTTPException(status_code=404, detail="Trigger not found on this routine")
    return trigger_id


CompanyGoalId = Annotated[str, Depends(_company_goal)]
CompanyProjectId = Annotated[str, Depends(_company_project)]
CompanyRoutineId = Annotated[str, Depends(_company_routine)]
CompanyRoutineTriggerId = Annotated[str, Depends(_company_routine_trigger)]

# ---- Routines ---------------------------------------------------------


@router.get("/routines")
async def list_routines(ctx: TeamsContext = Depends(_ctx)):
    """List routines in the caller's company.

    Archived routines are filtered out: ``delete_routine`` archives rather
    than deletes (upstream has no DELETE route), and Isol8 has no archive
    view — an archived row surfacing here would resurrect a "deleted"
    routine in the panel with controls that cannot work on it.
    """
    upstream = await _agents._admin().list_routines(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    enveloped = envelope_list(upstream, "routines")
    enveloped["routines"] = [r for r in enveloped["routines"] if r.get("status") != "archived"]
    return enveloped


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
    routine_id: CompanyRoutineId,
    body: CreateRoutineTriggerBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Attach a trigger (schedule cron, or webhook) to a routine.

    The schedule/webhook config lives on a separate trigger, not the routine
    row — the create dialog calls this after creating the routine itself.
    A webhook trigger forwards ``signing_mode`` / ``replay_window_sec`` and
    Paperclip returns the one-time ``{trigger, secretMaterial}`` — the caller
    must display ``secretMaterial.webhookUrl`` / ``webhookSecret`` immediately,
    it is never shown again (rotate is the only way to see a new one later).
    Upstream mints that ``webhookUrl`` from its in-VPC ``PAPERCLIP_API_URL``,
    which no provider can reach — ``stamp_fire_urls`` swaps in the public
    ingress URL, the same one the trigger projection carries as ``fireUrl``.

    Gated separately from ``create_routine``: the trigger IS the autonomy lever
    (a schedule fires on its own; a webhook lets an external caller fire the
    routine), so an ungated trigger endpoint would let a trial owner attach
    autonomy to a routine that already exists (e.g. one seeded before the
    gate, or created upstream).
    """
    await require_always_on_entitlement(ctx.owner_id)
    return stamp_fire_urls(
        await _agents._admin().create_routine_trigger(
            routine_id=routine_id,
            body=body.model_dump(exclude_none=True, by_alias=True),
            session_cookie=ctx.session_cookie,
        ),
        ctx.owner_id,
    )


@router.patch("/routines/{routine_id}")
async def patch_routine(
    routine_id: CompanyRoutineId,
    body: PatchRoutineBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch a routine.

    Gated on any fireability-INCREASING edit. Flipping ``status`` to ``active`` is
    the same lever as creating a routine — it is what makes the schedule fire — and
    the enable toggle PATCHes exactly ``{status: "active"}``. But activation is not
    the only lever: switching ``concurrency_policy`` to ``always_enqueue`` (never
    coalesce/skip an overlapping run) or ``catch_up_policy`` to
    ``enqueue_missed_with_cap`` (replay missed runs after downtime) raises scheduled
    dispatch VOLUME on an already-active routine without ever touching ``status``.
    Gating only ``status=="active"`` left that walk-around open — a trial owner
    could retime dispatch just as trigger-retiming let them retime cadence.

    Ranked on the TARGET value (the more-dispatching enum), so the gate fires only
    when the edit moves TOWARD more autonomy: narrowing a policy back to
    ``skip_if_active`` / ``coalesce_if_active`` / ``skip_missed`` is never blocked,
    matching the pause/rename/delete escape hatch. This needs no read of the current
    value — the frontend sends only changed fields, so the field's presence with a
    more-dispatching target IS the increase.

    Pause, rename, reassign and delete stay OPEN to trial owners on purpose:
    refusing those would trap someone with automation they cannot turn off, and
    only activation / more-dispatch reaches for autonomy.
    """
    increases_fireability = (
        body.status == "active"
        or body.concurrency_policy == "always_enqueue"
        or body.catch_up_policy == "enqueue_missed_with_cap"
    )
    if increases_fireability:
        await require_always_on_entitlement(ctx.owner_id)

    # Forward with exclude_UNSET (not exclude_none): the edit dialog sends only the
    # fields the user actually changed, and a CLEARED field arrives as an explicit
    # null (e.g. `description: null`). exclude_none would drop that null before
    # Paperclip saw it, so the clear was silently ignored and the dialog snapped back
    # to the old value. exclude_unset keeps client-sent fields — including explicit
    # nulls — while omitting fields the client never sent, so a partial PATCH stays
    # partial. The fireability gate above reads `body.status` (the model attribute),
    # not this dict, so it is unaffected; `extra="forbid"` still bars stray fields.
    result = await _agents._admin().patch_routine(
        routine_id=routine_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )

    return result


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: CompanyRoutineId, ctx: TeamsContext = Depends(_ctx)):
    """Delete a routine. Idempotent — upstream 404 is swallowed."""
    result = await _agents._admin().delete_routine(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return result


@router.post("/routines/{routine_id}/run")
async def run_routine(
    routine_id: CompanyRoutineId,
    body: dict[str, Any] = Body(default_factory=dict),
    ctx: TeamsContext = Depends(_ctx),
):
    """Manually fire a routine.

    Body is forwarded to upstream as-is — upstream's ``runRoutineSchema``
    accepts optional ``reason`` / ``inputOverrides`` and is the source of
    truth for validation.

    Firing a routine dispatches a run immediately server-side inside
    Paperclip, so this is a spend front door. The free-tier $3/day
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
    routine_id: CompanyRoutineId,
    ctx: TeamsContext = Depends(_ctx),
):
    """List run history for a routine."""
    upstream = await _agents._admin().list_routine_runs(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "runs")


@router.get("/routines/{routine_id}")
async def get_routine(routine_id: CompanyRoutineId, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single routine by id (the routine detail page).

    Webhook triggers are stamped with ``fireUrl`` — the public address the
    owner pastes into the provider's dashboard to ARM the trigger. Without it
    an already-existing trigger can only be armed through the API: the URL's
    shape is server-owned (PUBLIC_API_BASE + owner-id resolution) and upstream's
    own ``webhookUrl`` is the in-VPC host, so the panel cannot derive it.
    """
    return stamp_fire_urls(
        await _agents._admin().get_routine(
            routine_id=routine_id,
            session_cookie=ctx.session_cookie,
        ),
        ctx.owner_id,
    )


@router.get("/routines/{routine_id}/activity")
async def list_routine_activity(routine_id: CompanyRoutineId, ctx: TeamsContext = Depends(_ctx)):
    """A routine's activity — the ported stock RoutineDetail Activity section.

    Stock (``ui/src/api/routines.ts`` ``routinesApi.activity`` @
    v2026.916.1-isol8.2) has no routine-scoped route: the browser fans out
    ``GET /companies/{co}/activity?entityType=&entityId=`` over the routine,
    each of its triggers and each of its runs, then dedupes by id and sorts
    newest first. The BFF does that fan-out itself so the browser never names
    a company or an entity: the routine is fetched on the member session and
    must be in the caller's workspace (404 otherwise — a Paperclip user can
    belong to several companies), and every activity call is pinned to that
    company and to ids read off the routine and its runs.
    """
    admin = _agents._admin()
    routine = await admin.get_routine(routine_id=routine_id, session_cookie=ctx.session_cookie)
    if not isinstance(routine, dict) or routine.get("companyId") != ctx.company_id or not routine.get("id"):
        raise HTTPException(status_code=404, detail="Routine not found")
    runs = await admin.list_routine_runs(routine_id=routine["id"], session_cookie=ctx.session_cookie)
    entities = [("routine", routine["id"])]
    entities += [
        ("routine_trigger", t["id"]) for t in routine.get("triggers") or [] if isinstance(t, dict) and t.get("id")
    ]
    entities += [("routine_run", r["id"]) for r in runs if isinstance(r, dict) and r.get("id")]
    pages = await asyncio.gather(
        *(
            admin.list_activity(
                session_cookie=ctx.session_cookie,
                company_id=ctx.company_id,
                entity_type=kind,
                entity_id=entity_id,
            )
            for kind, entity_id in entities
        )
    )
    deduped = {e["id"]: e for page in pages for e in envelope_list(page, "events")["events"] if e.get("id")}
    # ISO-8601 timestamps sort lexically; stock sorts by Date.
    events = sorted(deduped.values(), key=lambda e: str(e.get("createdAt") or ""), reverse=True)
    return {"events": events}


# ---- Routine triggers + revisions (A2 — editable routines) ------------
#
# These make RoutineDetailPanel editable beyond delete+recreate: edit/delete
# a schedule trigger, rotate a webhook trigger's signing secret, and
# view/restore the routine's revision history.
#
# Trigger mutations upstream are keyed by the globally-unique trigger id
# (``/api/routine-triggers/{id}``); the BFF nests them under the routine for a
# RESTful UI shape and forwards only the trigger id to the admin client.
#
# Trial-gate posture (preserving ``require_always_on_entitlement`` — see
# patch_routine + create_routine_trigger above):
#   - Any trigger edit that could INCREASE fireability is gated: enabling
#     (``enabled=true``, arms the schedule like ``status="active"``), retiming
#     (``cron_expression`` / ``timezone``), or loosening webhook auth
#     (``signing_mode`` / ``replay_window_sec``). Gating only ``enabled=true``
#     left a walk-around: a trial owner could retime an ALREADY-enabled schedule
#     to fire far more often, or drop signing to ``none``, and increase
#     autonomous/externally-triggered runs without ever setting ``enabled``.
#   - RESTORING a revision can revert a routine to an active snapshot with
#     enabled triggers → gated (a restore is an activation-adjacent lever).
#   - A PURE disable (``enabled=false``), a relabel, deleting a trigger, and
#     rotating a secret do NOT increase fireability and stay open — refusing them
#     would trap a trial owner with automation they can't turn off, matching the
#     pause/rename/delete escape hatch on routines.


@router.patch("/routines/{routine_id}/triggers/{trigger_id}")
async def patch_routine_trigger(
    routine_id: RoutineId,
    trigger_id: CompanyRoutineTriggerId,
    body: UpdateRoutineTriggerBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Edit a routine trigger (cron / timezone / label / enable / webhook signing).

    Gated on any edit that could INCREASE fireability — enabling the trigger
    (``enabled=true``, the same autonomy lever as ``patch_routine(status="active")``),
    retiming its schedule (``cron_expression`` / ``timezone``), or loosening its
    webhook auth (``signing_mode`` / ``replay_window_sec``). Gating only
    ``enabled=true`` was a hole: a trial owner with an ALREADY-enabled trigger
    could PATCH a tighter cron or ``signing_mode="none"`` WITHOUT ``enabled`` and
    drive more autonomous/externally-triggered runs around the gate. Only a pure
    disable (``enabled=false``) or a relabel stays open — the escape hatch, which
    can never grant autonomy.
    """
    increases_fireability = (
        body.enabled is True
        or body.cron_expression is not None
        or body.timezone is not None
        or body.signing_mode is not None
        or body.replay_window_sec is not None
    )
    if increases_fireability:
        await require_always_on_entitlement(ctx.owner_id)

    # Forward with exclude_UNSET (not exclude_none): the edit dialog sends only the
    # fields the user changed, and CLEARING a label arrives as an explicit null
    # (``label: null``). exclude_none would drop that null before Paperclip saw it,
    # so the clear was silently ignored and the label snapped back on refresh.
    # exclude_unset keeps client-sent fields — including explicit nulls — while
    # omitting fields the client never sent, so a partial PATCH stays partial. The
    # fireability gate above reads model attributes (``body.enabled`` etc.), not this
    # dict, so it is unaffected; ``extra="forbid"`` still bars stray fields.
    result = await _agents._admin().patch_routine_trigger(
        trigger_id=trigger_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )

    return result


@router.delete("/routines/{routine_id}/triggers/{trigger_id}")
async def delete_routine_trigger(
    routine_id: RoutineId,
    trigger_id: CompanyRoutineTriggerId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete a routine trigger. Idempotent — upstream 404 is swallowed.

    Ungated: removing a schedule can only REDUCE autonomy, never grant it.
    """
    result = await _agents._admin().delete_routine_trigger(
        trigger_id=trigger_id,
        session_cookie=ctx.session_cookie,
    )
    return result


@router.post("/routines/{routine_id}/triggers/{trigger_id}/rotate-secret")
async def rotate_routine_trigger_secret(
    routine_id: RoutineId,
    trigger_id: CompanyRoutineTriggerId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Rotate a webhook trigger's signing secret.

    Returns upstream's ``{trigger, secretMaterial, ...}`` — the new secret is
    shown once. Ungated: rotating the secret of an existing trigger is a
    management action, not an activation lever (it does not create autonomy).
    ``secretMaterial.webhookUrl`` is rewritten to the public ingress URL (see
    ``create_routine_trigger``).
    """
    return stamp_fire_urls(
        await _agents._admin().rotate_routine_trigger_secret(
            trigger_id=trigger_id,
            session_cookie=ctx.session_cookie,
        ),
        ctx.owner_id,
    )


@router.get("/routines/{routine_id}/revisions")
async def list_routine_revisions(
    routine_id: CompanyRoutineId,
    ctx: TeamsContext = Depends(_ctx),
):
    """List a routine's revision history (for the detail page's Revisions view)."""
    upstream = await _agents._admin().list_routine_revisions(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(upstream, "revisions")


@router.post("/routines/{routine_id}/revisions/{revision_id}/restore")
async def restore_routine_revision(
    routine_id: CompanyRoutineId,
    revision_id: RoutineId,
    ctx: TeamsContext = Depends(_ctx),
):
    """Restore a routine to a prior revision snapshot.

    Gated on ``require_always_on_entitlement``: a restore can revert the routine
    to an ``active`` snapshot with enabled triggers, which is activation-adjacent
    — the same reason ``patch_routine(status="active")`` is gated. A trial owner
    who can't run scheduled automation shouldn't be able to re-arm it via restore.

    Restoring a snapshot that names a since-deleted webhook trigger makes
    upstream RECREATE it with a fresh secret and return the one-time material as
    ``secretMaterials`` — an array, a third shape (see ``stamp_fire_urls``). Its
    ``webhookUrl`` is built from the same in-VPC host as create/rotate, so it is
    swapped here too: it is the "shown once, never again" value, and shipping the
    unusable one would strand whoever surfaces it.
    """
    await require_always_on_entitlement(ctx.owner_id)
    return stamp_fire_urls(
        await _agents._admin().restore_routine_revision(
            routine_id=routine_id,
            revision_id=revision_id,
            session_cookie=ctx.session_cookie,
        ),
        ctx.owner_id,
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
async def get_goal(goal_id: CompanyGoalId, ctx: TeamsContext = Depends(_ctx)):
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
    goal_id: CompanyGoalId,
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
async def delete_goal(goal_id: CompanyGoalId, ctx: TeamsContext = Depends(_ctx)):
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
async def get_project(project_id: CompanyProjectId, ctx: TeamsContext = Depends(_ctx)):
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
    project_id: CompanyProjectId,
    body: PatchProjectBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch a project.

    ``exclude_unset`` (not ``exclude_none``) so an explicit ``null`` reaches
    upstream: clearing the description sends ``description: null``, which
    ``exclude_none`` dropped, so the clear silently reverted.
    """
    return await _agents._admin().patch_project(
        project_id=project_id,
        body=body.model_dump(exclude_unset=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )


@router.delete("/projects/{project_id}")
async def delete_project(project_id: CompanyProjectId, ctx: TeamsContext = Depends(_ctx)):
    """Delete a project.

    ``_delete`` treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_project(
        project_id=project_id,
        session_cookie=ctx.session_cookie,
    )
