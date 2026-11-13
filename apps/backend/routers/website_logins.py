"""Website sign-ins: the workspace store every agent's browser fills from.

The runtime (the bridge's vault token -- never the hook token, which the agent's shell holds):
list the metadata, resolve one login at fill time, and file / poll /
cancel an ask when a page wants a login or a code nobody has saved. The human side of an ask
is an Activity item (``core/services/activity.py``), so its answer rides the Activity action
route.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.services import website_logins as store

internal_router = APIRouter()


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
    # that sends the person to the browser, and which browser the run is in.
    hint: str | None = Field(default=None, max_length=160)
    surface: str = Field(default="cloud", pattern="^(cloud|mac)$")


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
            surface=body.surface,
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
