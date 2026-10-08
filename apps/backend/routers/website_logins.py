"""Website sign-ins: the workspace store every agent's browser fills from.

Humans (Clerk, org admins only): list / add / edit / delete saved logins, list them per site
beside the workspace browser's own sign-ins (``/sites``), and sign the browser out of a site.
Values go in, metadata comes out -- no route ever returns a password or a cookie.

The runtime (the bridge's vault token -- never the hook token, which the agent's shell holds):
list the metadata, resolve one login at fill time, and file / poll /
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


class UpdateLoginBody(BaseModel):
    username: str | None = Field(default=None, max_length=store.MAX_FIELD)
    password: str | None = Field(default=None, max_length=store.MAX_SECRET)


class SignOutBody(BaseModel):
    site: str = Field(min_length=1, max_length=253)


@router.get("/sites")
async def list_sites(auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    return {"sites": await store.list_sites(resolve_owner_id(auth))}


@router.post("/sites/sign-out", status_code=204)
async def sign_out_site(body: SignOutBody, auth: AuthContext = Depends(get_current_user)):
    """Sign the workspace browser out of one site (browser_control.sign_out). 409 with a
    reason when it cannot be done right now (an agent is using the browser, a save is running)."""
    from core.services import browser_control

    require_org_admin(auth)
    site = body.site.strip().lower()
    if not store.is_site(site):
        raise HTTPException(status_code=400, detail="Enter a site like example.com.")
    try:
        await browser_control.sign_out(resolve_owner_id(auth), site, actor=auth.user_id)
    except browser_control.BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.patch("/{login_id}")
async def update_login(login_id: str, body: UpdateLoginBody, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    try:
        updated = await store.update_login(
            resolve_owner_id(auth), login_id, username=body.username, password=body.password, actor=auth.user_id
        )
    except store.LoginError as exc:
        raise _bad(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Login not found.")
    return updated


@router.delete("/{login_id}", status_code=204)
async def delete_login(login_id: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    if not await store.delete_login(resolve_owner_id(auth), login_id, actor=auth.user_id):
        raise HTTPException(status_code=404, detail="Login not found.")


# ---- runtime (vault token) ------------------------------------------------------------------


async def _vault(owner_id: str, request: Request) -> None:
    """Bridge-only: the owner's vault token (``agentcore_runtime.derive_vault_token``), which
    lives only in the bridge's memory (delivered on every backend invoke,
    ``runtime_transport._vault_header``). The hook token is refused on purpose:
    the agent's shell holds it, and these routes hand out passwords and 2FA codes."""
    import hmac

    from core.repositories import container_repo
    from core.services.agentcore_runtime import derive_vault_token

    presented = request.headers.get("Authorization", "").removeprefix("Bearer ")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    expected = derive_vault_token(owner_id, row["id"]) if row.get("id") else ""
    if not (expected and presented and hmac.compare_digest(presented.encode(), expected.encode())):
        raise HTTPException(status_code=401, detail="invalid vault token")


class ResolveBody(BaseModel):
    agentId: str = Field(default="", max_length=128)


class RequestBody(BaseModel):
    kind: str = Field(pattern="^(login|code)$")
    origin: str = Field(max_length=2048)
    agentId: str = Field(max_length=128)
    roomId: str | None = Field(default=None, max_length=64)
    threadRootMessageId: str | None = Field(default=None, max_length=64)
    # ``code`` only: the sign-in page's heading ("Check your text messages"), quoted in the line
    # that sends the person to the browser.
    hint: str | None = Field(default=None, max_length=160)


@internal_router.get("/{owner_id}")
async def runtime_list(owner_id: str, request: Request):
    await _vault(owner_id, request)
    return {
        "logins": [
            {k: r[k] for k in ("id", "origin", "username", "createdAt")} for r in await store.list_logins(owner_id)
        ]
    }


@internal_router.post("/{owner_id}/logins/{login_id}/resolve")
async def runtime_resolve(owner_id: str, login_id: str, body: ResolveBody, request: Request):
    await _vault(owner_id, request)
    found = await store.resolve_login(owner_id, login_id, agent_id=body.agentId)
    if found is None:
        raise HTTPException(status_code=404, detail="Login not found.")
    return found


@internal_router.post("/{owner_id}/requests", status_code=201)
async def runtime_request(owner_id: str, body: RequestBody, request: Request):
    await _vault(owner_id, request)
    try:
        return await store.create_request(
            owner_id,
            kind=body.kind,
            origin=body.origin,
            agent_id=body.agentId,
            room_id=body.roomId,
            thread_root_message_id=body.threadRootMessageId,
            hint=body.hint,
        )
    except (store.LoginError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc


@internal_router.get("/{owner_id}/requests/{request_id}")
async def runtime_poll(owner_id: str, request_id: str, request: Request):
    await _vault(owner_id, request)
    found = await store.poll_request(owner_id, request_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    return found


@internal_router.post("/{owner_id}/requests/{request_id}/cancel", status_code=204)
async def runtime_cancel(owner_id: str, request_id: str, request: Request):
    await _vault(owner_id, request)
    if await store.get_request(owner_id, request_id) is not None:
        await store.cancel_request(owner_id, request_id)
