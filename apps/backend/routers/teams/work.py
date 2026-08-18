"""Teams BFF — Routines + Goals + Projects.

Three resources with identical-shape CRUD; bundled into one router
file because they're almost-pure proxies to upstream Paperclip with
no security hot edges. Each mutating handler validates with a
``_Strict`` body schema (``extra="forbid"``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends

from core.repositories import billing_repo
from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE
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

logger = logging.getLogger(__name__)


async def _is_nightly_ceo_routine(ctx: TeamsContext, routine_id: str) -> bool:
    """Whether ``routine_id`` is this owner's nightly CEO routine.

    Title-match via a single ``get_routine`` — a rare user action (toggling
    the routine), so the extra GET is fine. Best-effort: any upstream error is
    swallowed as "not the nightly routine" so the PATCH proxy never breaks.
    """
    try:
        routine = await _agents._admin().get_routine(
            routine_id=routine_id,
            session_cookie=ctx.session_cookie,
        )
    except Exception:  # noqa: BLE001 — never break the PATCH on a lookup failure
        logger.warning("nightly-CEO routine lookup failed for routine=%s", routine_id, exc_info=True)
        return False
    return isinstance(routine, dict) and routine.get("title") == NIGHTLY_CEO_ROUTINE_TITLE


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
    routine_id: str,
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
    routine_id: str,
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

    # Nightly CEO loop opt-out: persist a DURABLE opt-out on the
    # billing row when the owner pauses/archives the nightly routine, and clear
    # it on re-activate. Billing survives a full reprovision (the container +
    # Paperclip company are wiped, the billing row is not), so this flag is what
    # keeps the owner's opt-out honored after a company wipe — the arm step
    # reads it on the fresh company and declines to re-seed. A full reprovision
    # DELETES the routine, so create_enabled_schedule_routine's title-skip
    # backstop cannot fire on the fresh company; the durable flag is then the
    # ONLY thing between the owner's opt-out and a silent re-arm. That is why the
    # opt-OUT write below is durable (propagated), not best-effort.
    #
    # Resolve identity BEFORE the mutation (the title is the identity; a combined
    # status+rename PATCH would hide it afterward). The pause is attempted FIRST
    # so it always takes effect upstream even if the billing write then fails —
    # the owner can always stop the loop; only persisting the preference retries.
    is_nightly = False
    if body.status in {"paused", "archived", "active"}:
        is_nightly = await _is_nightly_ceo_routine(ctx, routine_id)

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

    if is_nightly:
        if body.status in {"paused", "archived"}:
            # Opt-OUT is durable, NOT best-effort. Swallowing a failure would
            # return success with the flag unset and let a later reprovision
            # re-arm the loop the owner just turned off. Propagating makes the
            # caller retry; the pause already took effect upstream and the write
            # is idempotent, so a retry only re-attempts the flag.
            await billing_repo.set_nightly_ceo_opted_out(ctx.owner_id, True)
        elif body.status == "active":
            # Opt-IN clear stays best-effort: a failed clear leaves opt-out True,
            # which only ever PREVENTS a re-arm (the safe direction), so it must
            # never fail an otherwise-successful re-activate.
            try:
                await billing_repo.set_nightly_ceo_opted_out(ctx.owner_id, False)
            except Exception:  # noqa: BLE001 — opt-in clear is best-effort
                logger.warning("nightly-CEO opt-in clear failed for owner=%s", ctx.owner_id, exc_info=True)

    return result


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a routine. Idempotent — upstream 404 is swallowed.

    Nightly CEO loop opt-out: deleting the nightly routine is an
    opt-out gesture just like pausing it (the Teams Routines UI offers delete
    alongside pause), so persist the durable ``nightly_ceo_opted_out`` flag.
    Without it, a later full reprovision — which changes the company_id and so
    invalidates the arm cache — would silently re-create the routine the owner
    explicitly deleted. Identity is read BEFORE the delete, and the durable flag
    is written BEFORE the delete too: a delete is destructive, so a write-after
    design could not heal on retry — ``_is_nightly_ceo_routine`` can no longer
    identify the (now-gone) routine, so a swallowed billing failure would drop
    the opt-out permanently. Writing first means a flag failure aborts before
    the routine vanishes (retry stays possible), and a flag set for a routine
    whose delete then fails only ever errs toward NOT re-arming.
    """
    is_nightly = await _is_nightly_ceo_routine(ctx, routine_id)
    if is_nightly:
        # Durable opt-out, written BEFORE the destructive delete and NOT
        # best-effort (see docstring). A silent failure would let a later
        # reprovision re-create the routine the owner deleted.
        await billing_repo.set_nightly_ceo_opted_out(ctx.owner_id, True)
    result = await _agents._admin().delete_routine(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
    return result


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
    routine_id: str,
    trigger_id: str,
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

    # Nightly CEO loop opt-out: disabling the nightly routine's
    # trigger stops its schedule just like pausing the routine, so persist the
    # durable opt-out — else a later reprovision would silently re-arm the loop.
    # The routine row survives a trigger edit, so identity stays resolvable and
    # the flag is written AFTER the disable succeeds (unlike delete_routine,
    # whose routine identity vanishes). Durable on this stop direction: a failed
    # flag write PROPAGATES (the disable already took effect upstream; a retry
    # only re-attempts the idempotent flag).
    is_nightly = False
    if body.enabled is False:
        is_nightly = await _is_nightly_ceo_routine(ctx, routine_id)

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

    if is_nightly:
        await billing_repo.set_nightly_ceo_opted_out(ctx.owner_id, True)

    return result


@router.delete("/routines/{routine_id}/triggers/{trigger_id}")
async def delete_routine_trigger(
    routine_id: str,
    trigger_id: str,
    ctx: TeamsContext = Depends(_ctx),
):
    """Delete a routine trigger. Idempotent — upstream 404 is swallowed.

    Ungated: removing a schedule can only REDUCE autonomy, never grant it.

    Nightly CEO loop opt-out: deleting the nightly routine's
    trigger stops its schedule just like deleting or pausing the routine, so
    persist the durable ``nightly_ceo_opted_out`` flag — else a later reprovision
    would silently re-arm the loop. The routine row survives a trigger delete, so
    identity stays resolvable and the flag is written AFTER the delete succeeds
    (durable / propagated on this stop direction).
    """
    is_nightly = await _is_nightly_ceo_routine(ctx, routine_id)
    result = await _agents._admin().delete_routine_trigger(
        trigger_id=trigger_id,
        session_cookie=ctx.session_cookie,
    )
    if is_nightly:
        await billing_repo.set_nightly_ceo_opted_out(ctx.owner_id, True)
    return result


@router.post("/routines/{routine_id}/triggers/{trigger_id}/rotate-secret")
async def rotate_routine_trigger_secret(
    routine_id: str,
    trigger_id: str,
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
    routine_id: str,
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
    routine_id: str,
    revision_id: str,
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


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Delete a project.

    ``_delete`` treats a 404 as already-gone (idempotent), so a
    double-delete from a stale UI is a no-op rather than an error.
    """
    return await _agents._admin().delete_project(
        project_id=project_id,
        session_cookie=ctx.session_cookie,
    )
