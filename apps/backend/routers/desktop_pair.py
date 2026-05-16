"""Desktop pair / revoke endpoints (Clerk-authed).

Backs the two-node desktop architecture (spec:
docs/superpowers/specs/2026-05-15-desktop-node-architecture-migration-design.md).

POST /api/v1/desktop/pair
    Idempotently pre-pair the caller's two Mac-side devices (Tauri-mac +
    openclaw CLI subprocess) into their container's ``paired.json``.
    Request only carries the two PEM public keys; command sets are derived
    server-side from ``DESKTOP_MAC_COMMANDS`` / ``DESKTOP_CLI_COMMANDS``
    (the client never proposes its own command list).

DELETE /api/v1/desktop/devices/{device_id}
    Revoke a paired device. Removes it from ``paired.json`` AND force-closes
    any live API-Gateway WS session keyed by deviceId (security-review HIGH
    fix for stolen-Mac scenarios).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.containers import get_workspace
from core.repositories import container_repo
from core.services.desktop_pair import (
    EfsWorkspaceAdapter,
    pair_desktop_devices,
    revoke_desktop_device,
)
from core.services.node_proxy import force_close_device

router = APIRouter(prefix="/desktop", tags=["desktop"])


class PairRequest(BaseModel):
    tauriMacPubKey: str = Field(
        ...,
        min_length=1,
        description="PEM-encoded Ed25519 public key for the Tauri-mac process.",
    )
    cliPubKey: str = Field(
        ...,
        min_length=1,
        description="PEM-encoded Ed25519 public key for the openclaw CLI subprocess.",
    )


class PairResponse(BaseModel):
    gatewayUrl: str
    gatewayToken: str
    deviceIdMac: str
    deviceIdCli: str
    commandsMac: list[str]
    commandsCli: list[str]


def _workspace_adapter(owner_id: str) -> EfsWorkspaceAdapter:
    return EfsWorkspaceAdapter(
        owner_id=owner_id,
        workspace=get_workspace(),
        container_repo=container_repo,
        gateway_ws_url=settings.WS_GATEWAY_URL,
    )


@router.post("/pair", response_model=PairResponse)
async def post_pair(
    body: PairRequest,
    ctx: AuthContext = Depends(get_current_user),
) -> PairResponse:
    """Idempotently pre-pair the caller's two Mac devices."""
    owner_id = resolve_owner_id(ctx)
    # AuthContext has no display_name; use user_id as the base. The service
    # appends " | Isol8 Mac" / " | Isol8 Desktop (CLI)" suffixes itself.
    display_name_base = ctx.user_id

    try:
        result = await pair_desktop_devices(
            user_id=ctx.user_id,
            owner_id=owner_id,
            display_name_base=display_name_base,
            tauri_mac_pubkey=body.tauriMacPubKey,
            cli_pubkey=body.cliPubKey,
            workspace=_workspace_adapter(owner_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return PairResponse(
        gatewayUrl=result.gateway_url,
        gatewayToken=result.gateway_token,
        deviceIdMac=result.device_id_mac,
        deviceIdCli=result.device_id_cli,
        commandsMac=list(result.commands_mac),
        commandsCli=list(result.commands_cli),
    )


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    ctx: AuthContext = Depends(get_current_user),
) -> None:
    """Revoke a paired device and force-close its live WS session."""
    owner_id = resolve_owner_id(ctx)
    await revoke_desktop_device(
        device_id=device_id,
        workspace=_workspace_adapter(owner_id),
        force_close_device=force_close_device,
    )
