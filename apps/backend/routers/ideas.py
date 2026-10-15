"""Ideas: the list, Start, dismiss, and an agent's suggest_idea (spec 2026-10-14 §4.4).
See ``core.services.ideas``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from core.auth import AuthContext, get_current_user
from core.services import ideas
from core.services.container_auth import _require_hook_token

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


@router.post("/internal/ideas/suggestions/{owner_id}")
async def suggest_idea(owner_id: str, body: ideas.Suggestion, request: Request):
    """The agent's ``suggest_idea`` tool (hook token of THIS owner): the idea joins the workspace's
    Ideas list; it is never posted to chat."""
    await _require_hook_token(owner_id, request)
    return await ideas.suggest(owner_id, body)
