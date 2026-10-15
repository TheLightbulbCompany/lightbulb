"""Ideas: the catalog, Start and dismiss (spec 2026-10-14 §4.4). See ``core.services.ideas``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.services import ideas

router = APIRouter()


@router.get("/ideas")
async def list_ideas(auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await ideas.list_ideas(auth)


@router.post("/ideas/{idea_id}/start")
async def start_idea(idea_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await ideas.start(auth, idea_id)


@router.post("/ideas/{idea_id}/dismiss", status_code=204)
async def dismiss_idea(idea_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    await ideas.dismiss(auth, idea_id)
