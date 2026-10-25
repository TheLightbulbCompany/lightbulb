"""Hosted apps on the free network: a logged-in computer's route link, its cross-task door, the
wake-up page's routes and the workspace bridge's app-mail notice (``network_route``,
``app_wake``). Mounted at ``/api/v1``."""

import hmac
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import app_wake, network_route

router = APIRouter()

_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_APP = re.compile(r"^[a-z0-9.-]{1,80}$")


@router.websocket("/gateway/network")
async def route_link(websocket: WebSocket) -> None:
    """The ``bv`` door's path cap admits ``/api/v1/gateway/*``: the daemon dials here."""
    await network_route.serve(websocket)


class RouteOp(BaseModel):
    key: str = Field(..., max_length=64)
    generation: str = Field(..., max_length=64)
    op: str = Field(..., max_length=32)
    params: dict
    timeoutS: float = Field(..., gt=0, le=30)


@router.post("/network/internal/route", include_in_schema=False)
async def route_op(body: RouteOp, request: Request):
    """Another task forwards an op to the socket this task holds."""
    import asyncio

    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(403, detail="forbidden")
    try:
        result = await network_route.local_call(body.key, body.generation, body.op, body.params, body.timeoutS)
    except asyncio.TimeoutError:
        return {"ok": False, "error": {"code": "TIMEOUT"}}
    except RuntimeError as exc:
        from core.services.network_member import rpc_error

        return {"ok": False, "error": rpc_error(exc) or {"code": "UNAVAILABLE"}}
    return {"ok": True, "result": result}


class WakeKey(BaseModel):
    key: str = Field(..., min_length=1, max_length=4096)


def _app(app: str) -> str:
    if not _APP.match(app):
        raise HTTPException(404, detail="No wake-up request from that app.")
    return app


@router.get("/network/wakes")
async def list_wakes(ctx: AuthContext = Depends(get_current_user)):
    """The apps that asked to be woken (``wake_me``); a free account needs no workspace."""
    return {"wakes": await app_wake.list_wakes(ctx.user_id)}


@router.put("/network/wakes/{app}")
async def set_wake_key(app: str, body: WakeKey, ctx: AuthContext = Depends(get_current_user)):
    from core.services.messaging import ApiProblem

    try:
        return await app_wake.set_key(ctx.user_id, _app(app), body.key)
    except ApiProblem as exc:
        status = 404 if exc.error.code == "MEMBER_NOT_FOUND" else 400
        raise HTTPException(status, detail=exc.error.message) from exc


@router.delete("/network/wakes/{app}", status_code=204)
async def remove_wake(app: str, ctx: AuthContext = Depends(get_current_user)):
    await app_wake.remove(ctx.user_id, _app(app))
    return Response(status_code=204)


class AppMail(BaseModel):
    session: str = Field(..., pattern=_SESSION.pattern)


@router.post("/internal/messaging/network/app-mail/{owner_id}", status_code=204)
async def app_mail(owner_id: str, body: AppMail, request: Request):
    """A workspace's bridge saw new mail for one of its app sessions: content-free."""
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    await app_wake.notify_workspace(owner_id, body.session)
    return Response(status_code=204)
