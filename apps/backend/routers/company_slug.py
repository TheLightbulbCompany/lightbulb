"""The agent-email slug a new workspace picks at "Create your workspace".

Agents' addresses are ``{agent}@{slug}.{AGENT_EMAIL_DOMAIN}``. The owner chooses the slug
before the Clerk org exists: the page checks availability as they type, then HOLDS the slug
for the user, and only then creates the org and claims the hold for it (the
``organization.created`` webhook repeats the claim as a backstop). A lost race is a 409 and the
user picks again; there are no numeric suffixes (owner ruling 2026-09-24).

The hold, availability and source routes take a plain signed-in user: the caller may have no
tenant yet. A person (no org) claims a slug for their own tenant: that is the only way a person
gets a company slug and team address.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, company_slug_repo, user_repo
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import agent_email
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
    """Stash where this person came from. The ``organization.created`` webhook moves it onto a new
    org, and a person's own tenant adopts it now when its trial row already exists (else the trial
    row's creation adopts it). Unknown answers and touch keys are dropped, never refused: this must
    not stand between a person and a workspace."""
    from core.services import workspace_source

    await workspace_source.stash_for_user(
        auth.user_id,
        answer=body.answer,
        detail=body.detail,
        first_touch=body.first_touch,
        last_touch=body.last_touch,
        door=body.door or "sign_up",
    )
    if not auth.org_id and await billing_repo.get_by_owner_id(auth.user_id):
        await workspace_source.adopt_for_workspace(auth.user_id, auth.user_id)
    return {"ok": True}


class ClaimBody(HoldBody):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


@router.post("/slug/claim")
async def claim_slug(body: ClaimBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Hand the caller's hold to their tenant and name it: an org's page calls this with the NEW
    workspace's token right after creating it (the ``organization.created`` webhook repeats the claim
    as a backstop); a person claims for their own tenant. This is the one door that gives a person a
    company slug, and with it their team address. One slug per tenant: a second, different slug is a
    409."""
    owner_id = resolve_owner_id(auth)
    slug = company_slug_repo.normalize(body.slug)
    current = await company_slug_repo.get_by_owner(owner_id)
    if current and current.get("slug") != slug:
        raise HTTPException(status_code=409, detail="slug_already_claimed")
    if not await company_slug_repo.claim(slug=slug, user_id=auth.user_id, owner_id=owner_id):
        raise HTTPException(status_code=409, detail="slug_hold_lost")
    await WorkspaceRepo().set_name(owner_id, body.name)
    if owner_id == auth.user_id:  # a person: their address exists from now on (an org's is made on first use)
        user = await user_repo.get(owner_id) or {}
        await agent_email.ensure_team_address(owner_id, user.get("email"))
    return {"slug": slug}
