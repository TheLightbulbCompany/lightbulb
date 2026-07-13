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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.containers import get_workspace
from core.observability.product_analytics import capture_product_event
from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
    setup_issue_payload,
    stage_first_run_files,
)
from routers.teams import agents as _agents

logger = logging.getLogger(__name__)

router = APIRouter()
_ctx = _agents._ctx


class BriefBody(BaseModel):
    path: Literal["new", "grow"]
    input: str = Field(min_length=3, max_length=2000)
    detail: str | None = Field(default=None, max_length=500)


def _first_name(auth: AuthContext) -> str:
    local = (auth.email or "").split("@")[0].strip()
    return local.replace(".", " ").title() or "there"


def _label(body: BriefBody) -> str:
    """Issue-title label: strip scheme/path from grow URLs, else first words."""
    if body.path == "grow":
        host = body.input.split("//")[-1].split("/")[0].strip()
        return host or body.input[:40]
    words = body.input.split()
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "")


@router.post("/brief")
async def submit_brief(
    body: BriefBody,
    auth: AuthContext = Depends(get_current_user),
    ctx=Depends(_ctx),
):
    if auth.is_org_context:
        raise HTTPException(status_code=403, detail="personal_only")

    admin = _agents._admin()
    agents = await admin.list_agents(session_cookie=ctx.session_cookie, company_id=ctx.company_id)
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

    try:
        await admin.create_issue(
            company_id=ctx.company_id,
            body=setup_issue_payload(label=_label(body), friday_agent_id=friday_id),
            session_cookie=ctx.session_cookie,
        )
    except Exception:
        # Best-effort: the show still works from BRIEF.md alone (Friday's
        # bootstrap step 2 tolerates a missing Set-up issue by creating it).
        logger.exception("onboarding: Set-up issue creation failed for %s", ctx.owner_id)

    capture_product_event(
        distinct_id=auth.user_id,
        event="first_run_brief_submitted",
        properties={"path": body.path},
    )
    return {"status": "staged", "friday_agent_id": friday_id}
