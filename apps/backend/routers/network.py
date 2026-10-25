"""The workspace on the free ``lightbulb`` peer-to-peer network.

``/api/v1/network``: ``lightbulb login`` and ``lightbulb invite --agents``
(``network_login``; the CLI's calls carry no Clerk token, a signature over a one-time challenge
instead; the browser's confirm is Clerk), proof renewal, and the doorbell. Keys, codes and links are never
logged or echoed beyond the one answer that hands them out.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from core.auth import AuthContext, get_current_user
from core.services import device_login, network_login, network_member

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
