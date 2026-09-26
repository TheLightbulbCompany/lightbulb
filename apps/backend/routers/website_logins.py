"""Website logins: the workspace store every agent's browser fills from.

Humans (Clerk, org admins only): list / add / delete. Values go in, metadata comes out --
no route ever returns a password.

The runtime (hook token): list the metadata, resolve one login at fill time, and file / poll /
cancel an ask when a page wants a login or a code nobody has saved. The human side of an ask
is an Activity item (``core/services/activity.py``), so its answer rides the Activity action
route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services import website_logins as store

router = APIRouter()
internal_router = APIRouter()


class AddLoginBody(BaseModel):
    origin: str = Field(max_length=2048)
    username: str = Field(max_length=store.MAX_FIELD)
    password: str = Field(max_length=store.MAX_SECRET)


def _bad(exc: store.LoginError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
async def list_logins(auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    return {"logins": await store.list_logins(resolve_owner_id(auth))}


@router.post("", status_code=201)
async def add_login(body: AddLoginBody, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    try:
        return await store.add_login(
            resolve_owner_id(auth),
            body.origin,
            body.username,
            body.password,
            added_by=auth.user_id,
            added_by_name=auth.email,
        )
    except store.LoginError as exc:
        raise _bad(exc) from exc


@router.delete("/{login_id}", status_code=204)
async def delete_login(login_id: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    if not await store.delete_login(resolve_owner_id(auth), login_id, actor=auth.user_id):
        raise HTTPException(status_code=404, detail="Login not found.")


# ---- runtime (hook token) -------------------------------------------------------------------


async def _hook(owner_id: str, request: Request) -> None:
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)


class ResolveBody(BaseModel):
    agentId: str = Field(default="", max_length=128)


class RequestBody(BaseModel):
    kind: str = Field(pattern="^(login|code)$")
    origin: str = Field(max_length=2048)
    agentId: str = Field(max_length=128)
    roomId: str | None = Field(default=None, max_length=64)
    threadRootMessageId: str | None = Field(default=None, max_length=64)


@internal_router.get("/{owner_id}")
async def runtime_list(owner_id: str, request: Request):
    await _hook(owner_id, request)
    return {
        "logins": [
            {k: r[k] for k in ("id", "origin", "username", "createdAt")} for r in await store.list_logins(owner_id)
        ]
    }


@internal_router.post("/{owner_id}/logins/{login_id}/resolve")
async def runtime_resolve(owner_id: str, login_id: str, body: ResolveBody, request: Request):
    await _hook(owner_id, request)
    found = await store.resolve_login(owner_id, login_id, agent_id=body.agentId)
    if found is None:
        raise HTTPException(status_code=404, detail="Login not found.")
    return found


@internal_router.post("/{owner_id}/requests", status_code=201)
async def runtime_request(owner_id: str, body: RequestBody, request: Request):
    await _hook(owner_id, request)
    try:
        return await store.create_request(
            owner_id,
            kind=body.kind,
            origin=body.origin,
            agent_id=body.agentId,
            room_id=body.roomId,
            thread_root_message_id=body.threadRootMessageId,
        )
    except (store.LoginError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc


@internal_router.get("/{owner_id}/requests/{request_id}")
async def runtime_poll(owner_id: str, request_id: str, request: Request):
    await _hook(owner_id, request)
    found = await store.poll_request(owner_id, request_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    return found


@internal_router.post("/{owner_id}/requests/{request_id}/cancel", status_code=204)
async def runtime_cancel(owner_id: str, request_id: str, request: Request):
    await _hook(owner_id, request)
    if await store.get_request(owner_id, request_id) is not None:
        await store.cancel_request(owner_id, request_id)
