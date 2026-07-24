"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief stages the funnel input as an OpenClaw
primitive (BRIEF.md in Friday's workspace, pre-boot) and seeds a
company-level goal + project for Friday to work against. One-shot per
COMPANY: a generation-fenced conditional ``funnel_state: not_started ->
submitted`` write on the paperclip-companies OWNER row (see
``core.repositories.paperclip_repo.claim_funnel_submission``) is the
marker, so a second member of the same org submitting concurrently cannot
seed a duplicate pair. Open to any org member, not just admins — see
``submit_brief`` for why an admin-only gate was rejected.

Onboarding truth is company-scoped, not per-container and not per-user: an
org shares one container AND one owner row, so onboarding happens once for
the whole org regardless of which member's tab wins the claim. ``ctx.owner_id``
resolves to the OWNER row via ``PaperclipRepo.resolve_owner_row`` — the row
``provision_org`` wrote and fenced with ``onboarding_generation`` at G2 — so
every member submitting against the same company writes the SAME row, even
though each member has their own separate paperclip-companies row for
credentials/session purposes.

Per the 2026-07-24 architecture decision (see
``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md`` §2),
this replaces the old ``containers.onboarded_at`` stamp (S2) and the
``__onboarded__`` sentinel (S3) entirely: ``GET /container/status`` now
reads ``funnel_state == "complete"`` off the SAME owner row instead. A
successful ``/brief`` submit here only advances the funnel to
``submitted`` — ``complete`` is set once the server-owned bootstrap
dispatcher (``core.services.bootstrap_dispatcher``) observes Friday's
first-run kickoff actually deliver (``chat.final``). This endpoint kicks
that dispatch off in the common case (the funnel UI only renders inside a
live chat surface, so the container has almost always already provisioned
by the time someone submits it); ``GET /container/status`` is the fallback
that dispatches once the runtime comes up if it was still cold here.

There is no first-run BOOTSTRAP.md any more — OpenClaw's boot-time
reconcile (``reconcileWorkspaceBootstrapCompletionState``) never surfaces a
seeded BOOTSTRAP.md into Friday's context once ``setupCompletedAt`` is
pre-marked (see ``core/services/friday_identity.py``'s docstring and
research §5 of the architecture doc's companion) — the file was inert.
The server-dispatched ``chat.send`` above replaces it as a first-class,
observed operation instead of a file Friday is guaranteed never to read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user
from core.containers import get_workspace
from core.observability.product_analytics import capture_product_event
from core.repositories.paperclip_repo import FunnelClaimResult, PaperclipRepo
from core.services.bootstrap_dispatcher import maybe_dispatch_bootstrap
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.onboarding_brief import (
    derive_goal_title,
    project_name_for,
    render_brief_md,
    seed_goal_and_project,
    stage_first_run_files,
)
from routers.teams import agents as _agents

logger = logging.getLogger(__name__)

router = APIRouter()
_ctx = _agents._ctx


class BriefBody(BaseModel):
    path: Literal["new", "grow"]
    input: str | None = Field(default=None, max_length=2000)
    detail: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_input(self) -> "BriefBody":
        """`new` may omit the idea; `grow` may not omit the website.

        An absent `input` on the `new` path is the "I don't know yet — you
        pick" button: the missing idea IS the signal, and BRIEF.md tells
        Friday to choose one. Blank-but-present is normalized to absent so a
        client posting "" gets that same brief rather than one quoting an
        empty string back at her.
        """
        cleaned = (self.input or "").strip()
        if not cleaned:
            if self.path == "grow":
                raise ValueError("input is required on the grow path")
            self.input = None
        elif len(cleaned) < 3:
            raise ValueError("input must be at least 3 characters")
        else:
            self.input = cleaned
        return self


def _first_name(auth: AuthContext) -> str:
    local = (auth.email or "").split("@")[0].strip()
    return local.replace(".", " ").title() or "there"


@router.post("/brief")
async def submit_brief(
    body: BriefBody,
    auth: AuthContext = Depends(get_current_user),
    ctx=Depends(_ctx),
):
    # One-shot per COMPANY, not per user: onboarding truth lives on the
    # paperclip-companies OWNER row (resolved from ctx.owner_id — org_id for
    # orgs, the personal user_id for personal owners), not on the calling
    # member's own row. A generation-fenced conditional write also means two
    # tabs submitting at once cannot both win the claim — exactly one does.
    #
    # Open to ANY org member, not just admins: the claim already prevents
    # duplicates, so the only consequence of a non-admin winning the race is
    # that they supply the provisional project name ("Priya's Company" for
    # an org the admin owns) — Friday renames it a turn later. An
    # admin-only gate was rejected because an org whose admin never opens
    # the app would be permanently stuck with no self-service recovery.
    repo = PaperclipRepo(table_name="paperclip-companies")
    # Strong read: this path reads the owner row then conditionally writes the
    # SAME row (the funnel claim), so it must see the latest onboarding_generation
    # / funnel_state. (get_onboarding_state's per-poll reads stay eventually
    # consistent -- see resolve_owner_row.)
    owner_row = await repo.resolve_owner_row(ctx.owner_id, consistent_read=True)
    if owner_row is None or not owner_row.onboarding_generation:
        # No owner row yet, or it predates the onboarding_generation fence
        # (should not happen post-G2 for any row provision_org just wrote —
        # defensive). Either way there is nothing to fence a claim against
        # yet; the client already retries on 409, same as the other
        # not-ready cases below.
        raise HTTPException(status_code=409, detail="not_ready")
    generation = owner_row.onboarding_generation

    claim_result = await repo.claim_funnel_submission(
        owner_row.user_id, onboarding_generation=generation, submitted_by=auth.user_id
    )
    if claim_result is FunnelClaimResult.ALREADY_SUBMITTED:
        # Durable: the row is still on the generation we just read, but
        # funnel_state had already moved past not_started -- a peer's
        # submission won (or the funnel already completed). No narrow
        # self-healing window to reason about here (unlike the old
        # sentinel): funnel_state is a single attribute on the row itself,
        # so there is no separate "record" step whose failure could leave a
        # peer seeing SEEDED-but-empty.
        return {"status": "already_submitted"}
    if claim_result is FunnelClaimResult.STALE_GENERATION:
        # The company was reprovisioned (new onboarding_generation) between
        # our read and our write -- our view of the owner row is stale, not
        # a duplicate submit. Retry: the client already retries on 409, same
        # as the other not_ready cases below.
        raise HTTPException(status_code=409, detail="not_ready")
    # claim_result is WON -- proceed.

    # Set once seed_goal_and_project returns -- used by the except block
    # below to decide whether the Paperclip rows need to be rolled back.
    goal_id: str | None = None
    project_id: str | None = None
    try:
        admin = _agents._admin()
        try:
            agents = await admin.list_agents(session_cookie=ctx.session_cookie, company_id=ctx.company_id)
        except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
            # Paperclip availability issue (API error, transport failure, or
            # credential load) — same set paperclip_openclaw_pairing treats as
            # transient. The funnel client retries on 409, not 500.
            logger.warning(
                "onboarding: list_agents failed for %s -- Paperclip not ready: %s",
                ctx.owner_id,
                e,
            )
            raise HTTPException(status_code=409, detail="not_ready") from e
        friday = next((a for a in agents if a.get("role") == "ceo"), None)
        if friday is None:
            # Paperclip seed hasn't landed Friday yet — client retries.
            raise HTTPException(status_code=409, detail="not_ready")
        friday_id = friday["id"]

        first_name = _first_name(auth)

        # Seed BEFORE staging the instruction files: the server-dispatched
        # kickoff (core.services.bootstrap_dispatcher) tells Friday to go to
        # work, so the rows must exist before it dispatches or she wakes
        # into a company with no project.
        try:
            goal_id, project_id = await seed_goal_and_project(
                admin=admin,
                session_cookie=ctx.session_cookie,
                company_id=ctx.company_id,
                friday_agent_id=friday_id,
                goal_title=derive_goal_title(path=body.path, user_input=body.input),
                project_name=project_name_for(first_name),
            )
        except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
            logger.warning("onboarding: seeding goal/project failed for %s: %s", ctx.owner_id, e)
            raise HTTPException(status_code=409, detail="not_ready") from e

        brief_md = render_brief_md(
            path=body.path,
            user_input=body.input,
            detail=body.detail,
            display_name=first_name,
            email=auth.email or "",
        )

        # Recorded BEFORE staging the workspace files, not after: this is
        # the generation-fenced write that actually decides who wins.
        # Staging is a visible side effect on Friday's NEXT boot -- if this
        # write raced a reprovision (generation moved on) and staging ran
        # first anyway, Friday could boot against dead goal/project ids.
        # Recording first means only the caller whose record_onboarding_ids
        # actually lands ever reaches the staging call below; a caller that
        # loses the guard raises here instead and never stages anything (see
        # the except block's cleanup).
        await repo.record_onboarding_ids(
            owner_row.user_id, onboarding_generation=generation, goal_id=goal_id, project_id=project_id
        )

        # No board seeding here: the first run is Friday's own research
        # session, and the board fills at the END of it with the three
        # next-move tasks she proposes (the server-dispatched kickoff tells
        # her to). Each of those runs later as its own session when the
        # owner picks it.
        stage_first_run_files(
            workspace=get_workspace(),
            owner_id=ctx.owner_id,
            friday_agent_id=friday_id,
            brief_md=brief_md,
        )
    except BaseException:
        # BaseException, not Exception: asyncio.CancelledError (client
        # disconnect mid-flight) inherits from BaseException, not Exception.
        # Skipping the revert here would strand the claim in ``submitted``
        # forever (there is no stale-steal safety net anymore -- the
        # generation fence replaces it, and a stuck ``submitted`` funnel
        # would permanently block the SAME generation from ever being
        # re-submitted). The bare `raise` below still re-raises, so
        # cancellation and interrupts propagate as before — this only adds
        # the cleanup.
        #
        # If seed_goal_and_project already returned ids, both Paperclip rows
        # exist but the failure happened during recording them against the
        # claim, or afterward while staging the workspace files — e.g. a
        # DynamoDB throttle on record_onboarding_ids, or an EFS write error
        # from stage_first_run_files. Neither is in the caught
        # (PaperclipApiError, httpx.HTTPError, RuntimeError) set above, so
        # both reach here directly. Left alone, a client retry would win a
        # fresh claim and seed a SECOND goal/project pair for the same
        # company.
        #
        # The same path also catches the case where the company was
        # reprovisioned mid-flight (a new onboarding_generation minted
        # underneath us): record_onboarding_ids is generation-fenced, so its
        # write fails (ConditionalCheckFailedException) instead of stamping
        # this caller's ids onto the new generation's fresh row. That
        # routes us here with OUR OWN goal_id/project_id -- the ones to roll
        # back -- while revert_funnel_submission below is ALSO
        # generation-fenced and simply no-ops against the new generation
        # (nothing of ours to revert on a row that's already moved on).
        #
        # Roll both back: delete the project, then the goal, then revert the
        # claim. Each step runs independently of the others' success — a
        # failure deleting the project must not skip the goal delete or the
        # claim revert — and none of them may raise past this block, or a
        # cleanup failure would replace the ORIGINAL error instead of the
        # caller seeing it.
        if goal_id is not None and project_id is not None:
            try:
                await admin.delete_project(session_cookie=ctx.session_cookie, project_id=project_id)
            except Exception as cleanup_err:
                logger.error(
                    "onboarding: failed to roll back project %s for company %s after post-seed failure: %s",
                    project_id,
                    ctx.company_id,
                    cleanup_err,
                )
            try:
                await admin.delete_goal(session_cookie=ctx.session_cookie, goal_id=goal_id)
            except Exception as cleanup_err:
                logger.error(
                    "onboarding: failed to roll back goal %s for company %s after post-seed failure: %s",
                    goal_id,
                    ctx.company_id,
                    cleanup_err,
                )

        # Caveat: awaiting inside a cancellation handler is not guaranteed
        # to complete — this revert (and the deletes above) can themselves
        # be cancelled (e.g. on a hard shutdown). A stuck ``submitted``
        # funnel with no goal/project has no self-service recovery today
        # (mirrors the old sentinel's stale-steal caveat); best-effort is
        # the accepted posture, not a guarantee.
        try:
            await repo.revert_funnel_submission(owner_row.user_id, onboarding_generation=generation)
        except Exception as cleanup_err:
            logger.error(
                "onboarding: failed to revert funnel claim for company %s after post-seed failure: %s",
                ctx.company_id,
                cleanup_err,
            )
        raise

    # funnel_state is already "submitted" (the claim above). It does NOT
    # advance to "complete" here -- that transition belongs to the bootstrap
    # dispatcher, which sets it once Friday's first-run kickoff is OBSERVED
    # to deliver (chat.final), not merely staged. GET /container/status's
    # `onboarded` field reads funnel_state=="complete" off this same owner
    # row.
    #
    # Kick the dispatch off now, in case the runtime is ALREADY up (the
    # common case: the funnel UI only renders inside a live chat surface,
    # so by the time someone submits it the container has almost always
    # already provisioned). Fire-and-forget: maybe_dispatch_bootstrap never
    # raises, and if the runtime is still cold it does nothing -- the next
    # GET /container/status poll that observes the runtime come up
    # re-evaluates and dispatches then (see that module's docstring).
    #
    # Pass the onboarding dict we already know rather than letting the
    # dispatcher re-read the row: `owner_row` was read (consistent) BEFORE
    # the claim above, and the claim's own CAS is the ONLY thing that
    # changed since -- funnel_state -> "submitted" and
    # onboarding_submitted_by -> auth.user_id, exactly the two fields
    # claim_funnel_submission's UpdateExpression sets. bootstrap_state/
    # bootstrap_attempts are untouched by the claim, so owner_row's
    # pre-claim values for those are still accurate. This is the
    # `submitted_by` the dispatcher needs to address the kickoff at the
    # ACTUAL submitter (not the row's PK, which for an org is the creator —
    # see bootstrap_dispatcher's module docstring) without a second
    # paperclip-companies read.
    asyncio.create_task(
        maybe_dispatch_bootstrap(
            ctx.owner_id,
            onboarding={
                "onboarding_generation": generation,
                "funnel_state": "submitted",
                "bootstrap_state": owner_row.bootstrap_state or "not_started",
                "bootstrap_attempts": owner_row.bootstrap_attempts or 0,
                "user_id": owner_row.user_id,
                "onboarding_submitted_by": auth.user_id,
            },
        )
    )

    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path, "no_idea": body.input is None},
    )
    return {
        "status": "staged",
        "friday_agent_id": friday_id,
        "goal_id": goal_id,
        "project_id": project_id,
    }
