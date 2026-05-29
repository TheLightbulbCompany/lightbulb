from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import connection_repo
from core.services import config_patcher
from core.services.credentials import apply_connection, registry

router = APIRouter(prefix="/connections", tags=["connections"])


class PasteBody(BaseModel):
    token: str


def _get_provider_or_404(provider: str) -> registry.Provider:
    try:
        return registry.get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")


async def _validate_token(p: registry.Provider, token: str) -> bool:
    if not p.validation:
        return True
    headers = {p.validation.auth_header: f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(p.validation.method, p.validation.url, headers=headers)
        return resp.status_code < 400
    except httpx.HTTPError:
        return False


@router.get("")
async def list_connections(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    rows = await connection_repo.list_for_owner(owner_id)
    connected = {r["provider"]: r.get("status", "connected") for r in rows}
    return {
        "connections": [
            {
                "provider": k,
                "display_name": v.display_name,
                "acquisition": v.acquisition,
                "status": connected.get(k, "disconnected"),
            }
            for k, v in registry.all_providers().items()
        ]
    }


@router.post("/{provider}")
async def connect_paste(provider: str, body: PasteBody, auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "paste_token":
        raise HTTPException(status_code=400, detail=f"{provider} is not a paste-key integration")
    if not await _validate_token(p, body.token):
        raise HTTPException(status_code=422, detail="token failed validation against the provider")
    await connection_repo.put(owner_id, provider, {"token": body.token}, status="connected")
    await apply_connection.apply_connection(owner_id, provider, {"token": body.token})
    return {"status": "connected", "provider": provider}


@router.delete("/{provider}")
async def disconnect(provider: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    await connection_repo.delete(owner_id, provider)
    if p.wiring.openclaw_paths:
        for op in p.wiring.openclaw_paths:
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), "")
    return {"status": "disconnected", "provider": provider}
