"""
Desktop app authentication via Clerk sign-in tokens.

Creates a one-time sign-in token that the desktop app's WebView
can consume to establish a Clerk session. This bridges the gap
between the system browser (where OAuth/passkeys work) and the
Tauri WKWebView (which needs its own Clerk session).
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import device_login, local_sessions
from core.services.desktop_pair import DesktopPermissionGrant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desktop"])

CLERK_API_URL = "https://api.clerk.com/v1"


@router.post("/desktop/sign-in-token")
async def create_sign_in_token(
    auth: AuthContext = Depends(get_current_user),
):
    """
    Create a one-time Clerk sign-in token for the desktop app.

    Called by the desktop-callback page after the user authenticates
    via Google OAuth in the system browser. The token is sent to the
    Tauri app via deep link, where the WebView consumes it to establish
    its own Clerk session.
    """
    clerk_secret = os.getenv("CLERK_SECRET_KEY")
    if not clerk_secret:
        raise HTTPException(status_code=500, detail="CLERK_SECRET_KEY not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CLERK_API_URL}/sign_in_tokens",
            headers={
                "Authorization": f"Bearer {clerk_secret}",
                "Content-Type": "application/json",
            },
            json={
                "user_id": auth.user_id,
                "expires_in_seconds": 60,
            },
        )

    if resp.status_code != 200:
        logger.error("Clerk sign-in token creation failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Failed to create sign-in token")

    data = resp.json()
    return {"token": data["token"]}


class DeviceStartRequest(BaseModel):
    tauriMacPubKey: str = Field(..., min_length=1, max_length=4096)
    cliPubKey: str = Field(..., min_length=1, max_length=4096)
    # The Mac's hostname, shown to the approver so a phished link names a Mac they don't know.
    machineName: str | None = Field(default=None, max_length=device_login.MACHINE_NAME_MAX)


class DeviceApproveRequest(BaseModel):
    userCode: str = Field(..., min_length=1, max_length=16)


class DevicePollRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)


# ponytail: start/poll are unauthenticated and not rate-limited -- the repo has no
# rate-limiting primitive for public routes (see core/services/workspace_join.py). Each call
# is one or two DynamoDB writes on a 10-minute TTL row, and a guessed code is 256 bits. Add an
# API Gateway route throttle or a per-IP limiter here if start is ever abused for write volume.
@router.post("/device/start")
async def device_start(body: DeviceStartRequest):
    """`lightbulb join` begins: no auth, the Mac's two public keys in, a code pair out."""
    return await device_login.start(body.tauriMacPubKey, body.cliPubKey, body.machineName)


@router.post("/device/describe")
async def device_describe(body: DeviceApproveRequest, ctx: AuthContext = Depends(get_current_user)):
    """The approve page's read: the machine name for this code (410 once it is gone)."""
    return await device_login.describe(body.userCode)


@router.post("/device/approve")
async def device_approve(body: DeviceApproveRequest, ctx: AuthContext = Depends(get_current_user)):
    """The signed-in person approves: pair and enroll the Mac as them, in their workspace."""
    from routers.desktop_pair import pair_devices

    owner_id = resolve_owner_id(ctx)
    row = await device_login.claim(body.userCode, owner_id, ctx.user_id)
    if row.get("status") == "approved":
        return {"status": "approved"}
    pair = await pair_devices(
        ctx,
        owner_id,
        row["tauriMacPubKey"],
        row["cliPubKey"],
        DesktopPermissionGrant(screen=False, shell=False, browser=False, files=False),
    )
    enrollment = await local_sessions.set_enrollment(
        ctx,
        local_sessions.EnrollmentRequest(device_id=pair.deviceIdMac, cli_device_id=pair.deviceIdCli, enabled=True),
    )
    await device_login.finish(
        row,
        ctx.user_id,
        {
            # The poll is unauthenticated: never hand it the workspace gateway token. The
            # desktop treats the field as optional and authenticates with its device keys.
            "pair": {**pair.model_dump(mode="json"), "gatewayToken": ""},
            "enrollment": enrollment.model_dump(mode="json"),
            "humanUserId": ctx.user_id,
            # What `lightbulb join` prints: "Connected as <name> in <workspace>." None when unknown.
            **await _approval_names(ctx.user_id, owner_id),
        },
    )
    return {"status": "approved"}


async def _approval_names(user_id: str, owner_id: str) -> dict:
    from core.services import clerk_admin, messaging

    try:
        name = await messaging.human_display_name(user_id)
    except Exception:  # noqa: BLE001 -- a label never blocks the approval
        name = None
    try:
        workspace = ((await clerk_admin.get_organization(owner_id)) or {}).get("name")
    except Exception:  # noqa: BLE001
        workspace = None
    # A failed lookup answers the raw user id: that is no name.
    return {"approverName": None if name == user_id else name, "workspaceName": workspace or None}


@router.post("/device/poll")
async def device_poll(body: DevicePollRequest):
    """No auth: pending, the stored pair + enrollment exactly once, then expired."""
    return await device_login.poll(body.code)
