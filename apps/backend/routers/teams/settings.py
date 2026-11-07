"""Teams BFF — workspace settings: ``name`` and ``description`` on the workspace META row.

PATCH whitelist allows only ``name`` and ``description`` (``PatchCompanySettingsBody``).
GET falls back to the Clerk org name when the workspace never set one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.repositories.paperclip_repo import PaperclipRepo
from core.services import workspace_name

from . import agents as _agents
from .deps import TeamsContext
from .schemas import PatchCompanySettingsBody

router = APIRouter()
_ctx = _agents._ctx


def _repo() -> PaperclipRepo:
    return PaperclipRepo(table_name="paperclip-companies")


@router.get("/settings")
async def get_settings(ctx: TeamsContext = Depends(_ctx)):
    ws = await _repo().get_workspace(ctx.owner_id)
    return {
        "name": await workspace_name.saved_name(ctx.owner_id) or "",
        "description": (ws.description if ws else None) or "",
    }


@router.patch("/settings")
async def patch_settings(body: PatchCompanySettingsBody, ctx: TeamsContext = Depends(_ctx)):
    await _repo().set_profile(ctx.owner_id, name=body.name, description=body.description)
    return await get_settings(ctx)
