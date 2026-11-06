"""The "paste your context" door -- /api/v1/company/context.

A pasted business context (what the owner already told Claude or ChatGPT) is merged into the
CEO's Hermes user-profile memory, ``memories/USER.md``, and nowhere else (owner ruling
2026-09-17: CEO only). See ``core.services.context_import`` for the merge, the budget and the
overflow spill, and spec ``2026-09-17-first-run-connectors-and-context-design.md`` §4.3.

POST is org-admin only: it writes into the agent that speaks for the whole workspace. GET is a
bare "has anything been imported" bit, readable by any member so the onboarding band can show
them the card as done or "Ask an admin".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import context_import

router = APIRouter()

#: Far above any real paste (Hermes keeps 1375 chars of it in USER.md; the rest is spilled), and
#: small enough that one request cannot balloon the profile.
MAX_CONTEXT_CHARS = 200_000


class ContextBody(BaseModel):
    text: str = Field(max_length=MAX_CONTEXT_CHARS)


async def _ceo_agent_id(owner_id: str) -> str:
    """``starter_agent_ids["ceo"]``, else the legacy roster's first seeded agent (the same
    fallback ``paperclip_repo`` uses for first-contact mail)."""
    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    ceo = company and ((company.starter_agent_ids or {}).get("ceo") or (company.seeded_agent_ids or [None])[0])
    if not ceo:
        raise HTTPException(
            status_code=409, detail="Lightbulb isn't set up in this workspace yet. Try again once setup finishes."
        )
    return ceo


@router.get("/context")
async def get_context(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    ceo = await _ceo_agent_id(owner_id)
    return {"imported": await run_in_thread(context_import.has_imported_context, owner_id, ceo)}


@router.post("/context")
async def post_context(body: ContextBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="Paste some context first.")
    ceo = await _ceo_agent_id(owner_id)
    try:
        return await run_in_thread(context_import.merge_context, owner_id, ceo, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Nothing in that paste could be saved.") from exc
