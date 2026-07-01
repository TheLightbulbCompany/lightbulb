"""Platform-ops API — operator console + owner-scoped agent access.

Dual auth: a Clerk platform-admin JWT (the admin UI) OR a platform_ops token
bound to settings.PLATFORM_OPS_OWNER_ID (the founder's agent). Default-deny.
"""

from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import get_current_user, require_platform_admin
from core.config import settings
from core.services import platform_ops_token
from core.services.ops import deploys, ecs_ops
from core.services.ops import envelope as env

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


async def require_ops_access(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Allow Clerk platform-admins OR the platform_ops owner token.

    Returns a short principal label ("admin" | "platform_ops"). Default-deny.
    """
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing credentials")
    token = creds.credentials

    # Agent path first — cheap local HS256 verify, no network.
    owner = settings.PLATFORM_OPS_OWNER_ID
    if owner:
        try:
            platform_ops_token.verify(token, expected_owner_id=owner)
            return "platform_ops"
        except (jwt.InvalidTokenError, jwt.ExpiredSignatureError, RuntimeError):
            pass  # fall through to the admin path

    # Admin path — Clerk JWT. get_current_user raises 401 on invalid tokens.
    try:
        auth = await get_current_user(credentials=creds)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid ops credentials")
    return require_platform_admin(auth) and "admin"


@router.get("/fleet")
async def get_fleet(_principal: str = Depends(require_ops_access)):
    counts, cstate, cerr = await env.read_source("ecs", ecs_ops.fleet_counts_live)
    grid, gstate, gerr = await env.read_source("ecs", ecs_ops.fleet_grid)
    errors = cerr + gerr
    state = env.OK if not errors else env.PARTIAL if (counts or grid) else env.UNAVAILABLE
    return env.envelope({"counts": counts, "grid": grid}, source_state=state, errors=errors)


@router.get("/deploys")
async def get_deploys(_principal: str = Depends(require_ops_access)):
    data, state, errors = await env.read_source("cloudformation", deploys.list_stacks)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.post("/token")
async def mint_ops_token(auth=Depends(require_platform_admin)):
    """Mint a platform_ops token for the configured platform owner.

    Admin-only (NOT self-renewal by a bare ops token). Delivered to the
    founder's container as a Secrets-Manager env var out of band.
    """
    if not settings.PLATFORM_OPS_OWNER_ID:
        raise HTTPException(status_code=409, detail="PLATFORM_OPS_OWNER_ID not configured")
    token = platform_ops_token.mint(settings.PLATFORM_OPS_OWNER_ID)
    return {"token": token, "owner_id": settings.PLATFORM_OPS_OWNER_ID, "kind": "platform_ops"}
