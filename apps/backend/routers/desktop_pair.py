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

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.services.connection_service import ConnectionService
from core.services.desktop_pair import (
    DesktopPermissionGrant,
    DeviceRegisteredToAnotherMember,
    pair_desktop_devices,
    revoke_desktop_device,
)
from core.repositories import container_repo
from core.services import mac_browser

logger = logging.getLogger(__name__)

UNPAIR_CLEAR_SECONDS = 15  # both clears together; the whole route stays under ~20 s

router = APIRouter(prefix="/desktop", tags=["desktop"])


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


async def _first_name(user_id: str) -> str | None:
    from core.services.clerk_admin import get_user

    try:
        user = await asyncio.wait_for(get_user(user_id), 3) or {}
    except Exception:  # noqa: BLE001 -- a pair never waits on Clerk
        return None
    return mac_browser.clean_name(user.get("first_name"))


@router.post("/pair", response_model=PairResponse)
async def post_pair(
    body: PairRequest,
    ctx: AuthContext = Depends(get_current_user),
) -> PairResponse:
    """Idempotently pre-pair the caller's two Mac devices."""
    return await pair_devices(
        ctx,
        resolve_owner_id(ctx),
        body.tauriMacPubKey,
        body.cliPubKey,
        (
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


async def pair_devices(
    ctx: AuthContext,
    owner_id: str,
    tauri_mac_pubkey: str,
    cli_pubkey: str,
    permissions: DesktopPermissionGrant | None,
) -> PairResponse:
    """Pair a Mac's two devices as ``ctx``'s member of ``owner_id``. Shared by ``/desktop/pair``
    and the device-login approve (``routers/desktop_auth.py``), which pairs on the CLI's behalf."""
    # The member's first name (Clerk), so an agent can say "browsing on Prasiddha's Mac";
    # the user id when Clerk has none. The service appends " | Lightbulb Mac" /
    # " | Lightbulb Desktop (CLI)" itself. A re-pair (every desktop sign-in) refreshes it.
    display_name_base = await _first_name(ctx.user_id) or ctx.user_id

    try:
        result = await pair_desktop_devices(
            user_id=ctx.user_id,
            owner_id=owner_id,
            display_name_base=display_name_base,
            tauri_mac_pubkey=tauri_mac_pubkey,
            cli_pubkey=cli_pubkey,
            permissions=permissions,
        )
    except DeviceRegisteredToAnotherMember as e:
        logger.warning(
            "desktop pair refused: device registered to another member owner=%s user=%s device=%s holder=%s",
            owner_id,
            ctx.user_id,
            e.device_id,
            e.other_user_id,
        )
        # The desktop matches on ``code`` to rotate its Keychain identity and pair once more.
        raise HTTPException(status_code=409, detail={"code": e.code, "deviceId": e.device_id})
    except ValueError as e:
        logger.warning("desktop pair rejected owner=%s user=%s: %s", owner_id, ctx.user_id, e)
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
    """Revoke a paired device and force-close its live WS session.

    Only the member who paired the device may unpair it; a workspace admin may unpair any.
    Anyone else gets 404, so a device id never reveals whose Mac it is."""
    owner_id = resolve_owner_id(ctx)

    async def _force_close_device(target_device_id: str) -> bool:
        # The relay pins a device's socket to whichever backend instance
        # accepted it, so there is no in-process map to pop any more —
        # deleting the presence row(s) is what force-closes it: the relay's
        # own 60s refresh notices the row is gone and closes 1008.
        deleted = await ConnectionService().delete_relay_rows_for_device(owner_id, target_device_id)
        return deleted > 0

    own = None if ctx.is_org_admin else ctx.user_id
    if own is not None and not await container_repo.mark_paired_device_revoking(owner_id, device_id, user_id=own):
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        # Revoked at once for everything but one thing: from here every command resolution
        # refuses this pair, except the backend's own wipe of the Mac agent Chrome, which runs
        # before the trust record goes.
        if own is None:
            await container_repo.mark_paired_device_revoking(owner_id, device_id)
        # One budget: the route stays well inside API Gateway's 30 s.
        results = await asyncio.wait_for(
            asyncio.gather(mac_browser.clear_device(owner_id, device_id), return_exceptions=True),
            UNPAIR_CLEAR_SECONDS,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("mac browser clear on unpair failed owner=%s: %s", owner_id, type(result).__name__)
    except Exception as exc:  # noqa: BLE001 -- revocation must never wait on the Mac
        logger.warning("mac browser sign-in clear on unpair failed owner=%s: %s", owner_id, type(exc).__name__)
    finally:
        await revoke_desktop_device(
            device_id=device_id,
            owner_id=owner_id,
            force_close_device=_force_close_device,
        )
