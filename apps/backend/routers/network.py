"""Lightbulb Connect: the sign-in issuer's routes (``/api/v1/network``).

A computer on the network proves its person's name with a 24 h proof
(``core/services/network_issuer.py``). It gets the first one through a typed device code
(``device_login`` rows with ``purpose: "network"``) and renews it with its own key.
Never log a proof.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from core.auth import AuthContext, get_current_user
from core.services import device_login, network_issuer

router = APIRouter(tags=["network"])


class StartRequest(BaseModel):
    member_key: str = Field(..., min_length=52, max_length=52)
    computer: str | None = Field(default=None, max_length=256)  # the hostname, display only

    @field_validator("member_key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not network_issuer.member_key_ok(v):
            raise ValueError("member_key must be 52 lowercase base32 characters")
        return v


class UserCodeRequest(BaseModel):
    user_code: str = Field(..., min_length=1, max_length=16)


class TokenRequest(BaseModel):
    device_code: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    proof: str = Field(..., min_length=1, max_length=network_issuer.MAX_TOKEN)
    at: int
    sig: str = Field(..., min_length=1, max_length=128)


# ponytail: start/token/refresh are unauthenticated and not rate-limited, like
# routers/desktop_auth.py's device routes (the repo has no public-route limiter).
@router.post("/device/start")
async def device_start(body: StartRequest):
    return await device_login.start_network(body.member_key, body.computer)


@router.post("/device/describe")
async def device_describe(body: UserCodeRequest, ctx: AuthContext = Depends(get_current_user)):
    return await device_login.describe(body.user_code, device_login.NETWORK)


@router.post("/device/approve")
async def device_approve(body: UserCodeRequest, ctx: AuthContext = Depends(get_current_user)):
    """The signed-in person vouches for this computer. No workspace needed: a free person
    gets a proof with no ``workspace``; a member gets their active one."""
    row = await device_login.claim(body.user_code, ctx.org_id or "", ctx.user_id, device_login.NETWORK)
    if row.get("status") != "approved":
        proof = await network_issuer.computer_proof(ctx.user_id, ctx.org_id, row["member_key"], int(time.time()))
        await device_login.finish(row, ctx.user_id, {"proof": proof})
    return {"status": "approved"}


@router.post("/device/token")
async def device_token(body: TokenRequest):
    """``{"pending": true}``, then ``{"proof"}`` exactly once, then 410."""
    polled = await device_login.poll(body.device_code, device_login.NETWORK)
    if polled["status"] == "pending":
        return {"pending": True}
    if polled["status"] == "approved":
        return {"proof": polled["proof"]}
    return JSONResponse(status_code=410, content={"detail": "This code expired."})


@router.post("/proof/refresh")
async def proof_refresh(body: RefreshRequest):
    try:
        return {"proof": await network_issuer.refresh(body.proof, body.at, body.sig, int(time.time()))}
    except HTTPException:
        raise
    except ValueError as exc:  # the claims no longer fit (e.g. the name is gone)
        raise HTTPException(status_code=401, detail="Proof refresh refused.") from exc
