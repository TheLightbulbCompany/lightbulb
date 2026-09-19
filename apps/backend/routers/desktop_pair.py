"""Desktop pair / revoke endpoints (Clerk-authed).

Backs the two-node desktop architecture.

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
from core.services.connection_service import ConnectionService
from core.services.desktop_pair import (
    DesktopPermissionGrant,
    pair_desktop_devices,
    revoke_desktop_device,
)
from core.services import local_sessions

router = APIRouter(prefix="/desktop", tags=["desktop"])


@router.put("/local-sessions/enrollment", response_model=local_sessions.EnrollmentResponse)
async def set_local_sessions_enrollment(
    body: local_sessions.EnrollmentRequest,
    ctx: AuthContext = Depends(get_current_user),
):
    return await local_sessions.set_enrollment(ctx, body)


class PairPermissions(BaseModel):
    screen: bool = False
    shell: bool = False
    browser: bool = False
    files: bool = False


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
    permissions: PairPermissions | None = Field(
        default=None,
        description=(
            "User-granted desktop command groups. Omitted means legacy full access "
            "for backward-compatible desktop clients; new clients send this explicitly."
        ),
    )


class PairResponse(BaseModel):
    gatewayUrl: str
    # The raw WebSocket gateway relay's door — the node clients dial THIS one (`bv-`
    # CloudFront/ALB). The backend TERMINATES that socket now (`gateway_relay`'s
    # `_serve_node_protocol`): it used to pipe frames verbatim to a second socket on the
    # owner's container, and an AgentCore runtime serves none. `gatewayUrl` above stays on the
    # API Gateway door: the local-session relay still needs its Management API and reads that
    # field, not this one.
    relayUrl: str
    gatewayToken: str
    # Container owner (org_id for an org, else the personal user_id). The
    # desktop embeds this in its self-minted device-key WS-upgrade token so the
    # authorizer knows which owner's containers row to read for the pubkey.
    ownerId: str
    deviceIdMac: str
    deviceIdCli: str
    commandsMac: list[str]
    commandsCli: list[str]


@router.post("/pair", response_model=PairResponse)
async def post_pair(
    body: PairRequest,
    ctx: AuthContext = Depends(get_current_user),
) -> PairResponse:
    """Idempotently pre-pair the caller's two Mac devices."""
    owner_id = resolve_owner_id(ctx)
    # AuthContext has no display_name; use user_id as the base. The service
    # appends " | Lightbulb Mac" / " | Lightbulb Desktop (CLI)" suffixes itself
    # (and normalises the retired brands' suffixes on an existing entry).
    display_name_base = ctx.user_id

    try:
        result = await pair_desktop_devices(
            user_id=ctx.user_id,
            owner_id=owner_id,
            display_name_base=display_name_base,
            tauri_mac_pubkey=body.tauriMacPubKey,
            cli_pubkey=body.cliPubKey,
            permissions=(
                DesktopPermissionGrant(
                    screen=body.permissions.screen,
                    shell=body.permissions.shell,
                    browser=body.permissions.browser,
                    files=body.permissions.files,
                )
                if body.permissions is not None
                else None
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # No live push: the trust record IS the containers row every reader already consults
    # (``machine_auth`` at the WS upgrade, ``declared_commands_for`` at connect), so a pair
    # takes effect on the device's very next connect with nothing to propagate.

    return PairResponse(
        gatewayUrl=result.gateway_url,
        relayUrl=settings.GATEWAY_RELAY_URL,
        gatewayToken=result.gateway_token,
        ownerId=owner_id,
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

    async def _force_close_device(target_device_id: str) -> bool:
        # The relay pins a device's socket to whichever backend instance
        # accepted it, so there is no in-process map to pop any more —
        # deleting the presence row(s) is what force-closes it: the relay's
        # own 60s refresh notices the row is gone and closes 1008.
        deleted = await ConnectionService().delete_relay_rows_for_device(owner_id, target_device_id)
        return deleted > 0

    await revoke_desktop_device(
        device_id=device_id,
        owner_id=owner_id,
        force_close_device=_force_close_device,
    )
