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
from core.containers import get_workspace
from core.repositories import container_repo
from core.services.desktop_pair import (
    DesktopPermissionGrant,
    EfsWorkspaceAdapter,
    pair_desktop_devices,
    revoke_desktop_device,
)
from core.services.desktop_pair_live import (
    push_desktop_pairing_live,
    push_desktop_revoke_live,
)
from core.services.node_proxy import force_close_device
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
    gatewayToken: str
    # Container owner (org_id for an org, else the personal user_id). The
    # desktop embeds this in its self-minted device-key WS-upgrade token so the
    # authorizer knows which owner's containers row to read for the pubkey.
    ownerId: str
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
            workspace=_workspace_adapter(owner_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Push the pairing into the RUNNING container's live trust store. Since the
    # OpenClaw 8.x image the gateway only reads the paired.json files at boot
    # (one-time SQLite import), so without this a pair against a running
    # container is inert until the next container roll and the desktop loops on
    # NOT_PAIRED. push_desktop_pairing_live never raises and skips cleanly when
    # no container is running; the extra try/except is belt-and-braces so no
    # future bug in the push can fail an already-committed pair.
    try:
        await push_desktop_pairing_live(
            owner_id=owner_id,
            device_ids={result.device_id_mac, result.device_id_cli},
        )
    except Exception:  # noqa: BLE001 — the pair is already committed; live push is best-effort
        pass

    return PairResponse(
        gatewayUrl=result.gateway_url,
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
    await revoke_desktop_device(
        device_id=device_id,
        workspace=_workspace_adapter(owner_id),
        force_close_device=force_close_device,
    )
    # Evict from the RUNNING gateway's SQLite trust store too. The boot
    # import is add-only (existing SQLite rows win), so the JSON removal
    # above alone never evicts the device — not even at the next container
    # boot. Best-effort; never raises.
    try:
        await push_desktop_revoke_live(owner_id=owner_id, device_id=device_id)
    except Exception:  # noqa: BLE001 — the revoke is already committed; eviction is best-effort
        pass
