"""Authenticated HTTP replacement for the retired browser WebSocket RPC branch."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.repositories import integrations_repo
from core.services import runtime_rpc as service
from core.services import messaging
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


@router.post("/credentials/u/{owner_id}")
async def runtime_credentials(
    owner_id: str, body: RuntimeCredentialRequest, request: Request, response: Response
) -> dict:
    """Vend short-lived OAuth access tokens to this workspace's Hermes bridge.

    One per connected backend-refreshed provider, keyed by its MCP server key (the
    bridge maps it to ``MCP_<KEY>_ACCESS_TOKEN`` in the turn's secret scope) or, for
    a provider with no MCP server (microsoft365, called with curl), its own key."""
    from core.services.container_auth import _require_hook_token

    await _require_hook_token(owner_id, request)
    roster = await messaging.load_agent_roster(owner_id)
    if body.agent_id not in {agent["id"] for agent in roster}:
        raise HTTPException(status_code=403, detail="agent_not_in_workspace")

    connected = {
        row["provider"]: row
        for row in await integrations_repo.list_for_owner(owner_id)
        if row.get("provider") and row.get("status", "connected") == "connected"
    }
    tokens: dict[str, str] = {}
    unavailable: list[str] = []
    for provider in registry.all_providers().values():
        if (
            provider.key not in connected
            or not provider.oauth
            or registry.refresh_owner_for(provider, connected[provider.key]) != "backend"
            or not registry.available_for(provider, owner_id)
        ):
            continue
        key = provider.mcp_remote.key if provider.mcp_remote else provider.key
        try:
            tokens[key] = await access_token.get(owner_id, provider.key)
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
    response.headers["Cache-Control"] = "no-store"
    return {"tokens": tokens, "unavailable": unavailable}


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
