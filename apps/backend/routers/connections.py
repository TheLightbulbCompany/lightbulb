from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.credentials import (
    apply_connection,
    integration_secret_store,
    oauth_dance,
    registry,
)
from core.services.credentials.materializers import MATERIALIZER_REMOVERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"])


def _retains_secret(p: registry.Provider) -> bool:
    """Whether the backend keeps the credential (in Secrets Manager).

    ``refresh_owner == "actor"`` means a CLI/keyring inside the container owns
    and self-refreshes the token (e.g. gog), so the backend stores nothing
    durable and the container EFS keyring is the sole custodian. Every other
    provider is backend-custodian: the secret is retained so it can be
    re-injected (marketplace slot-fill, reconnect)."""
    return p.refresh_owner != "actor"


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
    rows = await integrations_repo.list_for_owner(owner_id)
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
    credential = {"token": body.token}
    # Paste tokens are backend-custodian: store the credential in Secrets
    # Manager (KMS-encrypted at rest) — never in DynamoDB — then deliver the
    # literal into the container. Mark the integration connected only after
    # wiring succeeds, so status never reports a false "connected".
    await integration_secret_store.put(owner_id, provider, credential)
    try:
        await apply_connection.apply_connection(owner_id, provider, credential)
    except Exception:
        _remove_materialized_artifacts(owner_id, p, credential)
        await integration_secret_store.delete(owner_id, provider)
        raise HTTPException(
            status_code=502,
            detail="failed to apply the connection to your container; please retry",
        )
    await integrations_repo.put_status(owner_id, provider, "connected")
    return {"status": "connected", "provider": provider}


@router.delete("/{provider}")
async def disconnect(provider: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    p = _get_provider_or_404(provider)
    # Materializer-backed providers wrote credential files onto EFS; remove
    # those first so disconnect leaves no on-disk residue. The remover deletes
    # the artifact by owner/provider and does not need the secret value.
    if p.wiring.materializer:
        remover = MATERIALIZER_REMOVERS.get(p.wiring.materializer)
        if remover:
            # Some removers locate the on-disk artifact by a credential field
            # (e.g. toml_cli uses credential["cli"]), so fetch the retained
            # secret before deleting it. Actor-refresh providers have none
            # (get -> {}), and their removers (gog) ignore the credential.
            cred = await integration_secret_store.get(owner_id, provider) or {}
            remover(owner_id, provider, cred)
    # Drop the retained secret (no-op for actor providers, which never stored
    # one) and the status row, then blank any per-agent openclaw.json slots.
    await integration_secret_store.delete(owner_id, provider)
    await integrations_repo.delete(owner_id, provider)
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
    p = registry.get_provider(prov)
    # Retain the credential in Secrets Manager only when the backend is the
    # custodian. Actor-refresh providers (e.g. gog) self-refresh inside the
    # container, so the backend stores nothing and the EFS keyring written by
    # apply_connection is the sole durable copy. Mark connected only after
    # wiring succeeds.
    if _retains_secret(p):
        await integration_secret_store.put(owner_id, prov, credential)
    try:
        await apply_connection.apply_connection(owner_id, prov, credential)
    except Exception:
        _remove_materialized_artifacts(owner_id, p, credential)
        if _retains_secret(p):
            await integration_secret_store.delete(owner_id, prov)
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=apply_failed")
    await integrations_repo.put_status(owner_id, prov, "connected")
    return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?connected={prov}")
