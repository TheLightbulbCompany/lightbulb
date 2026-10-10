"""Start a Claude Code / Codex session on the caller's OWN paired Mac (Clerk-authed)."""

import asyncio
import hmac
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.auth import AuthContext, get_current_user, get_human_session, resolve_owner_id
from core.config import settings
from core.services import desktop_session_start as service
from core.services.desktop_execution import DesktopError

router = APIRouter(prefix="/desktop-sessions", tags=["desktop"])


async def _bounded(coro, seconds: float = 12):
    try:
        async with asyncio.timeout(seconds):
            return await coro
    except DesktopError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except TimeoutError:
        raise HTTPException(503, detail={"code": "desktop_request_timeout"}) from None


@router.get("/targets")
async def targets(ctx: AuthContext = Depends(get_current_user)):
    return await _bounded(service.list_targets(resolve_owner_id(ctx), ctx.user_id))


@router.get("/targets/{device_id}/options")
async def options(device_id: str, generation: str, ctx: AuthContext = Depends(get_current_user)):
    return await _bounded(service.options(resolve_owner_id(ctx), ctx.user_id, device_id, generation))


@router.post("/start", status_code=202)
async def start(
    body: dict = Body(...),
    source: Literal["ios", "web", "remote"] = "remote",
    ctx: AuthContext = Depends(get_current_user),
):
    return await _bounded(service.start(resolve_owner_id(ctx), ctx.user_id, body, source=source))


@router.post("/open/{session_id}", status_code=202)
async def open_in_terminal(session_id: str, body: dict = Body(...), ctx: AuthContext = Depends(get_human_session)):
    """The owner opens their own app session in a held terminal on its Mac. Never a teammate or an agent."""
    return await _bounded(service.open_in_terminal(ctx, session_id, body))


@router.post("/internal")
async def internal(request: Request):
    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, detail={"code": "invalid_request"}) from None
    return await _bounded(service.handle_peer(body), service.START_MS / 1000 + 8)
