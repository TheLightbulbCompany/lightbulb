"""Teams BFF — Company settings.

PATCH whitelist allows only ``display_name`` and ``description``.
All other company fields (status, billing, instance settings, branding
overrides that affect other tenants, adapter fields) stay
operator-controlled and cannot be mutated through this endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from . import agents as _agents
from .deps import TeamsContext
from .schemas import PatchCompanySettingsBody

router = APIRouter()
_ctx = _agents._ctx


@router.get("/settings")
async def get_settings(ctx: TeamsContext = Depends(_ctx)):
    """Return the company record for the settings panel."""
    return await _agents._admin().get_company(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )


@router.patch("/settings")
async def patch_settings(
    body: PatchCompanySettingsBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Patch the company record with the whitelisted subset only."""
    payload = body.model_dump(exclude_none=True, by_alias=True)
    return await _agents._admin().patch_company(
        company_id=ctx.company_id,
        body=payload,
        session_cookie=ctx.session_cookie,
    )
