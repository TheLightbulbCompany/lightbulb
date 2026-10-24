"""Settings -> Network: this workspace on the free ``lightbulb`` peer-to-peer network.

A parked runtime can outlast API Gateway's 30 s on ``/invite``; the client retries on 504 or
``RUNTIME_UNAVAILABLE``. The invite line is a credential: returned once, never logged or stored.
"""

from fastapi import APIRouter, Body, Depends, Response
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services import network_member

router = APIRouter()


class InviteRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)


@router.get("")
async def network_status(auth: AuthContext = Depends(get_current_user)):
    return {"connected": await network_member.sessions_exist(resolve_owner_id(auth))}


@router.post("/invite")
async def network_invite(
    response: Response,
    body: InviteRequest | None = Body(default=None),
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    name = ((body.name if body else None) or "").strip() or auth.email or auth.user_id
    result = await network_member.connect(owner_id, auth.user_id, name)
    response.headers["Cache-Control"] = "no-store"
    return {"line": result["line"], "expiresAt": result["expires_at"]}


@router.post("/reconnect")
async def network_reconnect(auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    await network_member.reconnect(resolve_owner_id(auth))
    return {}
