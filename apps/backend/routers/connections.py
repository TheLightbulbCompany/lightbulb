from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import connection_repo
from core.services import config_patcher
from core.services.credentials import apply_connection, oauth_dance, registry
from core.services.credentials.materializers import MATERIALIZER_REMOVERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])


async def _scrub_if_actor_refresh(owner_id: str, p: registry.Provider) -> None:
    """For actor-refresh providers the container self-refreshes its own token and
    is the sole durable custodian, so the backend keeps nothing once the
    credential has been applied. Called only AFTER a successful apply — the
    connection is already wired, so a scrub failure must never turn a successful
    connect into a failure; log and proceed (the secret can be re-scrubbed on a
    later reconnect, and worst case is over-retention, never a broken connect)."""
    if p.refresh_owner != "actor":
        return
    try:
        await connection_repo.scrub_secret(owner_id, p.key)
    except Exception:
        logger.warning(
            "failed to scrub retained secret for owner=%s provider=%s after apply; connection stays connected",
            owner_id,
            p.key,
            exc_info=True,
        )


def _remove_materialized_artifacts(owner_id: str, p: registry.Provider, credential: dict) -> None:
    """On a FAILED apply, remove any partially-materialized on-disk artifacts so a
    failed connect leaves no durable credential residue on EFS (the row is deleted
    separately). A materializer can write one file before a later step raises — e.g.
    gog's `credentials set` succeeds but `tokens import` fails — and without this the
    artifact lingers while the UI shows disconnected and no row remains to clean it up
    on a later disconnect. No-op for openclaw_paths/paste providers (no materializer)."""
    if not p.wiring.materializer:
        return
    remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer)
    if not remover:
        return
    try:
        remover(owner_id, p.key, credential)
    except Exception:
        logger.warning(
            "failed to remove materialized artifacts for owner=%s provider=%s during rollback",
            owner_id,
            p.key,
            exc_info=True,
        )


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
    require_org_admin(auth)
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
        _remove_materialized_artifacts(owner_id, p, {"token": body.token})
        await connection_repo.delete(owner_id, provider)
        raise HTTPException(
            status_code=502,
            detail="failed to apply the connection to your container; please retry",
        )
    await _scrub_if_actor_refresh(owner_id, p)
    return {"status": "connected", "provider": provider}


@router.delete("/{provider}")
async def disconnect(provider: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    # Materializer-backed providers wrote credential files onto EFS; remove
    # those before dropping the row so disconnect leaves no on-disk residue.
    if p.wiring.materializer:
        row = await connection_repo.get(owner_id, provider)
        if row:
            # A non-retention scrub (refresh_owner: actor) removes credential_enc
            # but keeps the row. The materializer remover deletes the on-disk
            # artifact by owner/provider and does not need the secret, so fall
            # back to an empty credential instead of failing the disconnect.
            cred = connection_repo.decrypt_credential(row) if row.get("credential_enc") else {}
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
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    if p.acquisition != "direct_oauth":
        raise HTTPException(status_code=400, detail=f"{provider} is not an OAuth integration")
    url = await oauth_dance.start(owner_id, provider, redirect_uri=_redirect_uri(provider))
    return {"authorize_url": url}


# Public callback — the OAuth provider redirects the (unauthenticated) browser here.
# Identity comes from the server-side state record, NOT from the request. Mounted at /api/v1/oauth/...
# No org-admin gate here: the single-use `state` it consumes could only have been
# minted by `authorize`, which is admin-gated — so this write path is admin-gated transitively.
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
        _remove_materialized_artifacts(owner_id, registry.get_provider(prov), credential)
        await connection_repo.delete(owner_id, prov)
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=apply_failed")
    await _scrub_if_actor_refresh(owner_id, registry.get_provider(prov))
    return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?connected={prov}")
