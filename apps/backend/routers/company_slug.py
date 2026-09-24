"""The agent-email slug a new workspace picks at "Create your workspace".

Agents' addresses are ``{agent}@{slug}.{AGENT_EMAIL_DOMAIN}``. The owner chooses the slug
before the Clerk org exists: the page checks availability as they type, then HOLDS the slug
for the user, and only then creates the org -- whose ``organization.created`` webhook hands
the hold to the workspace (``company_slug_repo.claim_hold``). A lost race is a 409 and the
user picks again; there are no numeric suffixes (owner ruling 2026-09-24).

Both routes take a plain signed-in user: the caller has no workspace yet.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.repositories import company_slug_repo
from core.services.company_identity import MAX_SLUG_LEN, slug_unavailable_reason

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
    reason = await slug_unavailable_reason(body.slug, user_id=auth.user_id)
    if reason in {"invalid", "reserved"}:
        raise HTTPException(status_code=400, detail=f"slug_{reason}")
    if reason == "taken" or not await company_slug_repo.hold(slug=body.slug, user_id=auth.user_id):
        raise HTTPException(status_code=409, detail="slug_taken")
    return {"slug": body.slug}
