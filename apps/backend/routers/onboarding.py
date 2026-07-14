"""First-run onboarding endpoints.

POST /api/v1/onboarding/brief stages the funnel input as OpenClaw
primitives (BRIEF.md + first-run BOOTSTRAP.md in Friday's workspace,
pre-boot) and seeds the "Set up {label}" Paperclip issue assigned to
Friday. One-shot: BRIEF.md existence is the marker; replays are 200
no-ops. Personal owners only (the funnel is trial/personal in v1 —
spec §2.1).
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
from core.repositories import billing_repo
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
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
    if auth.is_org_context:
        raise HTTPException(status_code=403, detail="personal_only")

    # The first-run flow is the no-card free tier: only a trial_local owner
    # gets the funnel + brief. Paid and org owners are out of v1 scope.
    account = await billing_repo.get_by_owner_id(ctx.owner_id)
    if (account or {}).get("subscription_status") != "trial_local":
        raise HTTPException(status_code=403, detail="trial_only")

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
    brief_md = render_brief_md(
        path=body.path,
        user_input=body.input,
        detail=body.detail,
        display_name=first_name,
        email=auth.email or "",
    )
    bootstrap_md = render_first_run_bootstrap(first_name=first_name)

    wrote = stage_first_run_files(
        workspace=get_workspace(),
        owner_id=ctx.owner_id,
        friday_agent_id=friday_id,
        brief_md=brief_md,
        bootstrap_md=bootstrap_md,
    )
    if not wrote:
        return {"status": "already_submitted"}

    # No board seeding here: the first run is Friday's own research session,
    # and the board fills at the END of it with the three next-move tasks
    # she proposes (BOOTSTRAP step 5). Each of those runs later as its own
    # session when the owner picks it.
    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path, "no_idea": body.input is None},
    )
    return {"status": "staged", "friday_agent_id": friday_id}
