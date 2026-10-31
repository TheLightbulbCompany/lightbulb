"""The workspace on the free ``lightbulb`` peer-to-peer network.

``/api/v1/network``: ``lightbulb login`` and ``lightbulb invite --agents``
(``network_login``; the CLI's calls carry no Clerk token, a signature over a one-time challenge
instead; the browser's confirm is Clerk), proof renewal, and the doorbell. Keys, codes and links are never
logged or echoed beyond the one answer that hands them out.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.auth import AuthContext, get_current_user
from core.services import device_login, network_login, network_member
from core.services.messaging import ApiProblem

router = APIRouter()
internal_router = APIRouter()


class _Signed(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    challenge: str = Field(..., min_length=1, max_length=64)
    sig: str = Field(..., min_length=1, max_length=128)


class DeviceStart(_Signed):
    purpose: str = Field(..., max_length=16)
    computer: str | None = Field(default=None, max_length=256)
    ticket: str | None = Field(default=None, max_length=8192)
    proof: str | None = Field(default=None, max_length=4096)  # agents: this computer's sign-in


class DeviceCode(BaseModel):
    userCode: str = Field(..., min_length=1, max_length=16)


class DevicePoll(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.post("/device/challenge")
async def device_challenge(response: Response):
    _no_store(response)
    return await network_login.challenge()


@router.post("/device/start")
async def device_start(body: DeviceStart, request: Request, response: Response):
    _no_store(response)
    ip = request.client.host if request.client else None  # ProxyHeaders: the caller's, not the ALB's
    return await network_login.start(
        body.key, body.purpose, body.challenge, body.sig, body.computer, body.ticket, ip, body.proof
    )


@router.post("/device/poll")
async def device_poll(body: DevicePoll, response: Response):
    _no_store(response)
    return await device_login.poll(body.code)


@router.post("/device/describe")
async def device_describe(body: DeviceCode, ctx: AuthContext = Depends(get_current_user)):
    return await network_login.describe(body.userCode)


@router.post("/device/approve")
async def device_approve(body: DeviceCode, ctx: AuthContext = Depends(get_current_user)):
    return await network_login.approve(ctx, body.userCode)


async def _paired_device(request: Request):
    """A paired Mac (EdDSA device token). A Clerk token gets 403: the hosted page holds the
    Clerk session and must never be able to fetch a join ticket."""
    from core.services.machine_auth import verify_upgrade

    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    device = await verify_upgrade(token, "openclaw-macos")
    if device is None:
        raise HTTPException(403, "Paired Mac required")
    return device


async def desktop_or_computer(request: Request) -> tuple[str, str]:
    """``(user_id, owner_id)`` of a paired Mac, or of a signed-in computer (``X-Lightbulb-Proof``).
    ``owner_id`` equals ``user_id`` for a person with no workspace."""
    if request.headers.get("X-Lightbulb-Proof"):
        ctx, _label = await network_login.computer_identity(request)
        return ctx.user_id, ctx.org_id or ctx.user_id
    device = await _paired_device(request)
    return device.user_id, device.owner_id


async def _workspace_person(user_id: str, owner_id: str) -> None:
    """403 unless ``user_id`` is one of the workspace's people (the sidecar's own gate)."""
    from core.services.website_logins import _humans_to_ask

    if user_id not in await _humans_to_ask(owner_id, None):
        raise HTTPException(403, "You are not in this workspace.")


@router.get("/team")
async def network_team(response: Response, who: tuple[str, str] = Depends(desktop_or_computer)):
    """Which workspace network this person's computer should be on. Stored state only: no RPC,
    so it never wakes the workspace."""
    _no_store(response)
    user_id, owner_id = who
    if owner_id == user_id:
        return {"workspace": None, "teamId": None, "userId": user_id, "name": None}
    await _workspace_person(user_id, owner_id)
    info = await network_member.team_info(owner_id)
    return {**info, "userId": user_id, "name": await network_login._name(user_id)}


@router.post("/join-line")
async def network_join_line(response: Response, who: tuple[str, str] = Depends(desktop_or_computer)):
    """A once link to the workspace's network for this person's computer. Never logged, never
    cached; the hosted page cannot reach it (a Clerk token is refused above)."""
    _no_store(response)
    user_id, owner_id = who
    if owner_id == user_id:
        raise HTTPException(409, {"code": "NO_WORKSPACE", "message": "You are not in a workspace."})
    await _workspace_person(user_id, owner_id)
    if not await network_member.sessions_exist(owner_id):
        raise HTTPException(409, {"code": "NO_SESSIONS", "message": "Your workspace has no agents on a network yet."})
    try:
        line = await network_member.mint_line(owner_id)
    except ApiProblem as exc:
        if exc.status_code != 503:
            raise
        return JSONResponse(exc.body(), status_code=503, headers={"Retry-After": "10", "Cache-Control": "no-store"})
    info = await network_member.team_info(owner_id)
    return {**line, "workspace": info["workspace"], "name": await network_login._name(user_id)}


@router.post("/device/approve-desktop")
async def device_approve_desktop(body: DeviceCode, response: Response, device=Depends(_paired_device)):
    """The paired Mac confirms its own ``lightbulb login --app`` code, so the person is not sent
    to a browser. Only a ``login`` code: an ``agents`` code would move the workspace."""
    _no_store(response)
    row = await device_login.live_row(body.userCode, network_login.KIND)
    if row.get("purpose") != "login":
        raise HTTPException(403, "This code is not a sign-in code.")
    ctx = AuthContext(user_id=device.user_id, org_id=device.org_id or None)
    return await network_login.approve(ctx, body.userCode)


@router.post("/leave")
async def network_leave(ctx: AuthContext = Depends(get_current_user)):
    """An admin takes this workspace's agents off their network, so ``lightbulb invite --agents``
    can add them to another."""
    from core.auth import require_org_admin, resolve_owner_id

    require_org_admin(ctx)
    await network_member.leave_network(resolve_owner_id(ctx))
    return {"state": "left"}


class ProofRefresh(_Signed):
    proof: str = Field(..., min_length=1, max_length=4096)


@router.post("/proof/refresh")
async def proof_refresh(body: ProofRefresh, response: Response):
    _no_store(response)
    return await network_login.refresh(body.key, body.challenge, body.sig, body.proof)


@router.post("/proof/revoke")
async def proof_revoke(body: ProofRefresh, response: Response):
    """``lightbulb logout``: the computer signs itself out everywhere before deleting its proof."""
    _no_store(response)
    return await network_login.logout(body.key, body.challenge, body.sig, body.proof)


@router.get("/issuer")
async def issuer():
    """The public key that signs sign-in proofs (for the engine's key ceremony and dev)."""
    from core.services import network_proof

    return network_proof.public_key()


class HoldNotice(BaseModel):
    """The bridge's content-free hold notice. Any other field (the held text) is refused."""

    model_config = ConfigDict(extra="forbid")

    holdId: str = Field(pattern=network_member.HOLD_ID_RE.pattern)
    state: Literal["open", "closed"]
    label: str = Field(default="", max_length=200)
    target: str = Field(default="", max_length=200)


@internal_router.post("/holds/{owner_id}", status_code=204)
async def network_hold_notice(owner_id: str, body: HoldNotice, request: Request):
    from routers.messaging import _require_hook_token

    await _require_hook_token(owner_id, request)
    await network_member.hold_notice(owner_id, body.model_dump())
    return Response(status_code=204)


@router.post("/wake", include_in_schema=False)
async def network_wake(request: Request):
    """The doorbell: no Clerk auth, ``Authorization: Bearer <owner>.<secret>`` is the credential
    (a header, never the path, which access logs record). No body either way."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    ok = await network_member.ring(token.strip() if scheme.lower() == "bearer" else "")
    return Response(status_code=202 if ok else 404)
