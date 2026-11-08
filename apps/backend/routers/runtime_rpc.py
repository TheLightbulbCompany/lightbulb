"""Authenticated HTTP replacement for the retired browser WebSocket RPC branch."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user
from core.repositories import integrations_repo
from core.services import runtime_rpc as service
from core.services import messaging, token_custody
from core.services.owner_lifecycle import OwnerLifecycleBusyError
from core.services.credentials import access_token, registry
from core.services.messaging import (
    RuntimeOperation,
    RuntimeOperationAccepted,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class RuntimeCredentialRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # "caller": a phone caller's thread (governance P2, the caller case): the model grant only, no owner tokens.
    principal: Literal["caller"] | None = None


class RuntimeModelRequest(RuntimeCredentialRequest):
    presented_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    force: bool = False

    @model_validator(mode="after")
    def _force_names_the_token(self) -> RuntimeModelRequest:
        # Spec 13 §5.2: a forced refresh happens only for the token the VM presents.
        if self.force and not self.presented_sha256:
            raise ValueError("force requires presented_sha256")
        return self


async def _authorize_vend(owner_id: str, agent_id: str, request: Request) -> None:
    from core.services.container_auth import _require_hook_token

    await _require_hook_token(owner_id, request)
    roster = await messaging.load_agent_roster(owner_id)
    if agent_id not in {agent["id"] for agent in roster}:
        raise HTTPException(status_code=403, detail="agent_not_in_workspace")


def _model(vend: token_custody.CodexVend) -> dict:
    return {
        "provider": token_custody.PROVIDER_KEY,
        "access_token": vend.access_token,
        "id_token": vend.id_token,
        "account_id": vend.account_id,
        "expires_at": vend.expires_at,
    }


@router.post("/credentials/u/{owner_id}")
async def runtime_credentials(
    owner_id: str, body: RuntimeCredentialRequest, request: Request, response: Response
) -> dict:
    """Vend short-lived OAuth access tokens to this workspace's Hermes bridge.

    One per connected backend-refreshed provider, keyed by its MCP server key (the
    bridge maps it to ``MCP_<KEY>_ACCESS_TOKEN`` in the turn's secret scope) or, for
    a provider with no MCP server (microsoft365, called with curl), its own key.

    Additive (spec 13 §5.1): ``expires_at`` ({key: epoch s}) when any token has a known expiry,
    and ``model`` -- the ChatGPT access token -- when the workspace's grant is backend-custodied.
    A payload without either is exactly today's."""
    await _authorize_vend(owner_id, body.agent_id, request)

    connected = {
        row["provider"]: row
        for row in await integrations_repo.list_for_owner(owner_id)
        if row.get("provider") and row.get("status", "connected") == "connected"
    }
    tokens: dict[str, str] = {}
    expires: dict[str, int] = {}
    unavailable: list[str] = []
    for provider in registry.all_providers().values():
        if (
            body.principal == "caller"
            or provider.key not in connected
            or not provider.oauth
            or registry.refresh_owner_for(provider, connected[provider.key]) != "backend"
            or not registry.available_for(provider, owner_id)
        ):
            continue
        key = provider.mcp_remote.key if provider.mcp_remote else provider.key
        try:
            tokens[key], expires_at = await access_token.get_with_expiry(owner_id, provider.key)
            if expires_at:
                expires[key] = expires_at
        except access_token.ReconnectRequired:
            unavailable.append(provider.key)  # not connected / no usable grant: the owner reconnects
        except Exception as exc:  # noqa: BLE001
            # One provider's failure must not drop every other provider's token for the turn --
            # an unexpected error included (a missing repo function once 500'd the whole call).
            logger.warning(
                "runtime credential unavailable owner=%s provider=%s error=%s: %s",
                owner_id,
                provider.key,
                type(exc).__name__,
                exc,
            )
            unavailable.append(provider.key)
    payload: dict = {"tokens": tokens, "unavailable": unavailable}
    # custody decides: codex_vend is None unless the row is custody=backend.
    try:
        vend = await token_custody.codex_vend(owner_id)
    except Exception as exc:  # noqa: BLE001 -- reconnect, busy, OpenAI down: the rest still vends
        logger.warning("runtime model unavailable owner=%s error=%s", owner_id, type(exc).__name__)
        unavailable.append(token_custody.PROVIDER_KEY)
    else:
        if vend is not None:
            payload["model"] = _model(vend)
            if vend.expires_at:
                expires["model"] = vend.expires_at
    if expires:
        payload["expires_at"] = expires
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.post("/credentials/u/{owner_id}/model")
async def runtime_model(owner_id: str, body: RuntimeModelRequest, request: Request, response: Response) -> dict:
    """Forced refresh for the bridge's F6 delegate: the VM got a 401 on the token it presents.
    Refreshes only if that token is still current; otherwise returns the newer one (N VMs, one
    refresh). 409 busy (lease held), 410 the grant is dead, 404 not backend-custodied."""
    await _authorize_vend(owner_id, body.agent_id, request)
    response.headers["Cache-Control"] = "no-store"
    try:
        vend = await token_custody.codex_vend(owner_id, presented_sha256=body.presented_sha256, force=body.force)
    except access_token.ReconnectRequired:
        raise HTTPException(status_code=410, detail={"code": "reconnect_required"}) from None
    except (OwnerLifecycleBusyError, token_custody.RefreshUnavailable):
        raise HTTPException(status_code=409, detail={"code": "busy"}) from None
    if vend is None:
        raise HTTPException(status_code=404, detail={"code": "not_brokered"})
    return {"model": _model(vend), "refreshed": vend.refreshed}


@router.post(
    "/rpc",
    response_model=RuntimeRpcResponse | RuntimeOperationAccepted,
    responses={202: {"model": RuntimeOperationAccepted}},
)
async def runtime_rpc(request: RuntimeRpcRequest, response: Response, auth: AuthContext = Depends(get_current_user)):
    status, payload = await service.execute(auth, request)
    response.status_code = status
    return payload


@router.get("/operations/{operation_id}", response_model=RuntimeOperation)
async def runtime_operation(operation_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.get_operation(auth, operation_id)
