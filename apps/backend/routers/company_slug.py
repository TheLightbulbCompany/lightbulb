"""The agent-email slug a new workspace picks at "Create your workspace".

Agents' addresses are ``{agent}@{slug}.{AGENT_EMAIL_DOMAIN}``. The owner chooses the slug
before the Clerk org exists: the page checks availability as they type, then HOLDS the slug
for the user, and only then creates the org and claims the hold for it (the
``organization.created`` webhook repeats the claim as a backstop). A lost race is a 409 and the
user picks again; there are no numeric suffixes (owner ruling 2026-09-24).

Both routes take a plain signed-in user: the caller has no workspace yet.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import company_slug_repo
from core.services.company_identity import MAX_SLUG_LEN, slug_format_problem, slug_unavailable_reason

router = APIRouter()


class HoldBody(BaseModel):
    slug: str = Field(min_length=1, max_length=MAX_SLUG_LEN)


@router.get("/slug-availability")
async def slug_availability(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    slug: str = Query(max_length=64),
):
    reason = await slug_unavailable_reason(slug, user_id=auth.user_id)
    return {"slug": slug, "available": reason is None, "reason": reason, "domain": settings.AGENT_EMAIL_DOMAIN}


@router.post("/slug/hold")
async def hold_slug(body: HoldBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    problem = slug_format_problem(body.slug)
    if problem:
        raise HTTPException(status_code=400, detail=f"slug_{problem}")
    # The conditional put decides "taken"; a read first would only race it.
    if not await company_slug_repo.hold(slug=body.slug, user_id=auth.user_id):
        raise HTTPException(status_code=409, detail="slug_taken")
    return {"slug": body.slug}


class SourceBody(BaseModel):
    """ "How did you first hear about us?" plus the browser's stored touches (T12)."""

    answer: str | None = Field(default=None, max_length=32)
    door: str | None = Field(default=None, max_length=32)
    detail: str | None = Field(default=None, max_length=500)
    first_touch: dict | None = None
    last_touch: dict | None = None


@router.post("/source")
async def stash_source(body: SourceBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Stash where this person came from before their workspace exists; the
    ``organization.created`` webhook moves it onto the workspace. Unknown answers and touch
    keys are dropped, never refused: this must not stand between a person and a workspace."""
    from core.services import workspace_source

    await workspace_source.stash_for_user(
        auth.user_id,
        answer=body.answer,
        detail=body.detail,
        first_touch=body.first_touch,
        last_touch=body.last_touch,
        door=body.door or "sign_up",
    )
    return {"ok": True}


@router.post("/slug/claim")
async def claim_slug(body: HoldBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """The page calls this with the NEW workspace's token right after creating it: a keyed,
    strongly consistent hand-over of the hold. The ``organization.created`` webhook repeats
    it as a backstop (idempotent)."""
    owner_id = resolve_owner_id(auth)
    if not await company_slug_repo.claim(slug=body.slug, user_id=auth.user_id, owner_id=owner_id):
        raise HTTPException(status_code=409, detail="slug_hold_lost")
    return {"slug": body.slug}
