"""Hosted apps on the free network: a logged-in computer's route link, its cross-task door, and
the apps page's routes (pinned computers, wake-ups) (``network_route``, ``app_wake``). Mounted at
``/api/v1``."""

import hmac
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import app_wake, network_route

router = APIRouter()

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


class PinRoute(BaseModel):
    clientId: str = Field(..., min_length=1, max_length=512)
    key: str = Field(..., min_length=1, max_length=64)


async def _computers(ctx: AuthContext) -> list[dict]:
    """The computers an app may be pinned to now: this person's online ones, newest first."""
    rows = await network_route._connections().routes_for_user(ctx.user_id)
    return [
        {"kind": "computer", "key": r["key"], "name": r.get("name") or "Your computer", "online": True}
        for r in sorted(rows, key=lambda r: -r["connectedAt"])
    ]


@router.get("/network/apps")
async def list_apps(ctx: AuthContext = Depends(get_current_user)):
    """The person's hosted apps (each pinned to one computer) and the computers online now."""
    pins = await network_route.list_pins(ctx.user_id)
    apps = [{"clientId": p["sk"], "app": p.get("app"), "client": p.get("client"), "route": p["route"]} for p in pins]
    return {"apps": apps, "routes": await _computers(ctx)}


@router.put("/network/apps/route")
async def pin_app(body: PinRoute, ctx: AuthContext = Depends(get_current_user)):
    """Pin an app to one of this person's computers; it must be online now (its name comes from
    its link)."""
    pin = await network_route.get_pin(ctx.user_id, body.clientId)
    if body.clientId.startswith("agentkey:"):
        from core.services import agent_keys

        keys = {k["keyId"]: k for k in await agent_keys.list_keys(ctx.org_id or ctx.user_id, ctx.user_id)}
        key = keys.get(body.clientId.removeprefix("agentkey:"))
        if key is None:
            raise HTTPException(404, detail="No such agent key of yours.")
        slug, client = f"agent-{key['keyId']}", key["name"]
    elif pin is None:
        raise HTTPException(404, detail="That app has not connected yet.")
    else:
        slug, client = pin.get("app") or "app", pin.get("client") or "An app"
    match = next((r for r in await _computers(ctx) if r["key"] == body.key), None)
    if match is None:
        raise HTTPException(409, detail="That computer is offline. Open it, then try again.")
    route = {k: match[k] for k in ("kind", "key", "name")}
    await network_route.set_pin(ctx.user_id, body.clientId, route, slug, client)
    return {"clientId": body.clientId, "route": route}
