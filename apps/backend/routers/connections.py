from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import connection_repo
from core.services import config_patcher
from core.services.credentials import apply_connection, oauth_dance, registry
from core.services.credentials.materializers import MATERIALIZER_REMOVERS

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
    scheme = p.validation.auth_scheme
    value = f"{scheme} {token}".strip() if scheme else token
    headers = {p.validation.auth_header: value}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            kwargs: dict = {"headers": headers}
            if p.validation.body is not None:
                kwargs["json"] = p.validation.body
            resp = await client.request(p.validation.method, p.validation.url, **kwargs)
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
    # Roll the row back if wiring fails, so status never reports a false "connected".
    try:
        await apply_connection.apply_connection(owner_id, provider, {"token": body.token})
    except Exception:
        await connection_repo.delete(owner_id, provider)
        raise HTTPException(
            status_code=502,
            detail="failed to apply the connection to your container; please retry",
        )
    return {"status": "connected", "provider": provider}


@router.delete("/{provider}")
async def disconnect(provider: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    # Materializer-backed providers wrote credential files onto EFS; remove
    # those before dropping the row so disconnect leaves no on-disk residue.
    if p.wiring.materializer:
        row = await connection_repo.get(owner_id, provider)
        if row:
            cred = connection_repo.decrypt_credential(row)
            remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer)
            if remover:
                remover(owner_id, provider, cred)
    await connection_repo.delete(owner_id, provider)
    if p.wiring.openclaw_paths:
        for op in p.wiring.openclaw_paths:
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), "")
    return {"status": "disconnected", "provider": provider}


def _redirect_uri(provider: str) -> str:
    # Server-derived, never caller-supplied. Must match the URI registered with
    # the provider and be identical in both authorize + callback legs.
    return f"{settings.PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback"


@router.get("/{provider}/authorize")
async def authorize(provider: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "direct_oauth":
        raise HTTPException(status_code=400, detail=f"{provider} is not an OAuth integration")
    url = await oauth_dance.start(owner_id, provider, redirect_uri=_redirect_uri(provider))
    return {"authorize_url": url}


# Public callback — the OAuth provider redirects the (unauthenticated) browser here.
# Identity comes from the server-side state record, NOT from the request. Mounted at /api/v1/oauth/...
oauth_router = APIRouter(prefix="/oauth", tags=["connections"])


@oauth_router.get("/{provider}/callback")
async def oauth_callback(provider: str, code: str, state: str):
    try:
        owner_id, prov, credential = await oauth_dance.exchange(state, code, redirect_uri=_redirect_uri(provider))
    except ValueError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=oauth_state")
    await connection_repo.put(owner_id, prov, credential, status="connected")
    # Roll the row back if wiring fails, so status never reports a false "connected".
    try:
        await apply_connection.apply_connection(owner_id, prov, credential)
    except Exception:
        await connection_repo.delete(owner_id, prov)
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=apply_failed")
    return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?connected={prov}")
