"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief stages the funnel input as OpenClaw
primitives (BRIEF.md + first-run BOOTSTRAP.md in Friday's workspace,
pre-boot) and seeds a company-level goal + project for Friday to work
against. One-shot per COMPANY: a PK-conditional onboarding claim in
``paperclip-companies`` (not BRIEF.md existence) is the marker, so a
second member of the same org submitting concurrently cannot seed a
duplicate pair. Open to any org member, not just admins — see
``submit_brief`` for why an admin-only gate was rejected.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user
from core.containers import get_workspace
from core.observability.product_analytics import capture_product_event
from core.repositories.paperclip_repo import PaperclipRepo
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
    if not await repo.claim_onboarding(ctx.company_id):
        return {"status": "already_submitted"}

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

        # No board seeding here: the first run is Friday's own research
        # session, and the board fills at the END of it with the three
        # next-move tasks she proposes (BOOTSTRAP step 5). Each of those
        # runs later as its own session when the owner picks it.
        stage_first_run_files(
            workspace=get_workspace(),
            owner_id=ctx.owner_id,
            friday_agent_id=friday_id,
            brief_md=brief_md,
            bootstrap_md=bootstrap_md,
        )

        # Recorded LAST: a claim carrying ids always means both Paperclip
        # rows exist and are linked.
        await repo.record_onboarding_ids(ctx.company_id, goal_id=goal_id, project_id=project_id)
    except BaseException:
        # BaseException, not Exception: asyncio.CancelledError (client
        # disconnect mid-flight) inherits from BaseException, not Exception,
        # and the onboarding claim has no stale-stealing or takeover path by
        # design. Skipping the release on cancellation would strand the
        # claim permanently, locking the company out of onboarding with no
        # automatic or self-service recovery. The bare `raise` below still
        # re-raises, so cancellation and interrupts propagate as before —
        # this only adds the cleanup.
        #
        # Caveat: awaiting inside a cancellation handler is not guaranteed
        # to complete — this release can itself be cancelled (e.g. on a hard
        # shutdown). This makes the release usually happen instead of never;
        # it is not a guarantee, and no shielded/retried release is
        # attempted here on purpose.
        await repo.release_onboarding_claim(ctx.company_id)
        raise

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
