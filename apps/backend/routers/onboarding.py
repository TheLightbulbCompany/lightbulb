"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief stages the funnel input as OpenClaw
primitives (BRIEF.md + first-run BOOTSTRAP.md in Friday's workspace,
pre-boot) and seeds a company-level goal + project for Friday to work
against. One-shot per COMPANY: a PK-conditional onboarding claim in
``paperclip-companies`` (not BRIEF.md existence) is the marker, so a
second member of the same org submitting concurrently cannot seed a
duplicate pair. Open to any org member, not just admins — see
``submit_brief`` for why an admin-only gate was rejected.

Whether the funnel shows at all is a per-CONTAINER fact, not per-user: an
org shares one container, so onboarding happens once for the whole org.
On success this stamps ``onboarded_at`` on the container row, which
``GET /container/status`` (already polled by the frontend) surfaces as
``onboarded`` — so once anyone in the org completes the funnel, no one on
that container sees it again. There is no separate onboarding-status
endpoint; the container the fact belongs to already carries it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user
from core.containers import get_workspace
from core.observability.product_analytics import capture_product_event
from core.repositories import container_repo
from core.repositories.paperclip_repo import OnboardingClaimResult, PaperclipRepo
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.onboarding_brief import (
    derive_goal_title,
    project_name_for,
    render_brief_md,
    render_first_run_bootstrap,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/brief")
async def submit_brief(
    body: BriefBody,
    auth: AuthContext = Depends(get_current_user),
    ctx=Depends(_ctx),
):
    # One-shot per COMPANY, not per user: the paperclip-companies table is
    # keyed per-user, so an org's second member has their own unmarked row
    # and would otherwise be served the funnel and seed a duplicate pair.
    # A PK conditional write also means two tabs submitting at once cannot
    # both seed — exactly one wins.
    #
    # Open to ANY org member, not just admins: the claim already prevents
    # duplicates, so the only consequence of a non-admin winning the race is
    # that they supply the provisional project name ("Priya's Company" for
    # an org the admin owns) — Friday renames it a turn later. An
    # admin-only gate was rejected because an org whose admin never opens
    # the app would be permanently stuck with no self-service recovery.
    repo = PaperclipRepo(table_name="paperclip-companies")
    # Fresh per request, not per user: a user retrying twice must be
    # distinguishable from themselves for the holder-guarded release below
    # to mean anything -- see claim_onboarding's docstring for the race this
    # closes (a stalled original holder releasing a claim a stealer since
    # took over).
    holder = uuid.uuid4().hex
    claim_result = await repo.claim_onboarding(ctx.company_id, holder=holder)
    if claim_result is OnboardingClaimResult.SEEDED:
        # Genuinely, durably done -- the only case that means this.
        return {"status": "already_submitted"}
    if claim_result is OnboardingClaimResult.IN_FLIGHT:
        # A peer holds a fresh (non-stale) claim right now. This is
        # transient, not done -- treating it as already_submitted would
        # wave THIS caller into a company that may end up with no goal or
        # project at all if the peer's own submit later fails. The client
        # already retries on 409, same as the other not_ready cases below.
        raise HTTPException(status_code=409, detail="not_ready")
    # claim_result is WON (fresh claim or a stale-steal takeover) -- proceed.

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

        # Seed BEFORE staging the instruction files: BOOTSTRAP.md tells
        # Friday to go to work, so the rows must exist before it lands or
        # she can wake into a company with no project.
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
        bootstrap_md = render_first_run_bootstrap(first_name=first_name, goal_id=goal_id, project_id=project_id)

        # Recorded BEFORE staging the workspace files, not after: this is
        # the holder-guarded write that actually decides who wins the steal
        # window. Staging is a visible side effect on Friday's NEXT boot --
        # if a stalled holder A staged first and only THEN lost the record
        # guard to stealer B, A's BOOTSTRAP.md (baked with A's now-deleted
        # ids) would already be on disk, and Friday could boot against dead
        # ids until B's own staging happened to overwrite it (a race, not a
        # guarantee -- B might boot first). Recording first means only the
        # caller whose record_onboarding_ids actually lands ever reaches
        # the staging call below; a caller that loses the record guard
        # raises here instead and never stages anything (see the except
        # block's cleanup).
        await repo.record_onboarding_ids(ctx.company_id, goal_id=goal_id, project_id=project_id, holder=holder)

        # No board seeding here: the first run is Friday's own research
        # session, and the board fills at the END of it with the three
        # next-move tasks she proposes (BOOTSTRAP step 7). Each of those
        # runs later as its own session when the owner picks it.
        stage_first_run_files(
            workspace=get_workspace(),
            owner_id=ctx.owner_id,
            friday_agent_id=friday_id,
            brief_md=brief_md,
            bootstrap_md=bootstrap_md,
        )
    except BaseException:
        # BaseException, not Exception: asyncio.CancelledError (client
        # disconnect mid-flight) inherits from BaseException, not Exception.
        # Skipping the release here would strand the claim: FIX 2 gives it a
        # stale-steal safety net (claim_onboarding takes over a >60s-old
        # claim with no seeded_at), but releasing promptly still lets an
        # immediate retry re-enter instead of waiting out that window. The
        # bare `raise` below still re-raises, so cancellation and interrupts
        # propagate as before — this only adds the cleanup.
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
        # The same path also catches the case where THIS caller stalled past
        # the stale window, got stolen from, and only now finished seeding:
        # record_onboarding_ids is holder-guarded, so its write fails
        # (ConditionalCheckFailedException) instead of stamping this
        # caller's ids over the new holder's claim. That routes us here with
        # OUR OWN goal_id/project_id -- the ones to roll back -- while the
        # new holder's claim and in-progress seed are untouched (our release
        # below carries our own now-stale `holder`, so it no-ops against
        # theirs).
        #
        # Roll both back: delete the project, then the goal, then release
        # the claim. Each step runs independently of the others' success —
        # a failure deleting the project must not skip the goal delete or
        # the claim release — and none of them may raise past this block,
        # or a cleanup failure would replace the ORIGINAL error instead of
        # the caller seeing it.
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
        # to complete — this release (and the deletes above) can themselves
        # be cancelled (e.g. on a hard shutdown). This makes recovery
        # usually happen instead of never; it is not a guarantee, and no
        # shielded/retried cleanup is attempted here on purpose — FIX 2's
        # stale-steal safety net covers what doesn't complete.
        try:
            await repo.release_onboarding_claim(ctx.company_id, holder=holder)
        except Exception as cleanup_err:
            logger.error(
                "onboarding: failed to release claim for company %s after post-seed failure: %s",
                ctx.company_id,
                cleanup_err,
            )
        raise

    # Mark the CONTAINER onboarded (not the user): an org shares one
    # container, so this is the per-org "the funnel is done" fact that
    # GET /container/status surfaces as `onboarded`, keeping the funnel from
    # ever showing again to anyone on this container -- including a second
    # org member whose own per-user marker is empty. ctx.owner_id is the
    # container owner (org owner for orgs), so the flag lands on the shared
    # row. Best-effort: the seed already succeeded, and if this write fails
    # the only fallback is that another member sees the funnel once more and
    # gets a harmless `already_submitted` (the claim blocks a duplicate seed).
    try:
        await container_repo.update_fields(ctx.owner_id, {"onboarded_at": _now_iso()})
    except Exception as e:
        logger.warning("onboarding: failed to stamp onboarded_at for %s: %s", ctx.owner_id, e)

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
