"""Marketplace seller-handle endpoints.

The publish wizard's pre-flight + the /settings page consume these. The
underlying validation + atomic-write rules live in
``core/services/seller_identity.py`` -- this router is the thin HTTP shim.

Endpoints (all under ``/api/v1/sellers``):

    GET  /sellers/me              -- 404 if unclaimed, 200+state if claimed
    POST /sellers/me/username     -- {username} -> 200+state on success,
                                     400/409 with structured ``error`` body
                                     on validation/cooldown/taken failures.

The publish gate in ``routers/marketplace_listings.py`` calls
``seller_identity.get_for_user`` directly rather than going through HTTP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user
from core.services import seller_identity


router = APIRouter(prefix="/api/v1/sellers", tags=["marketplace"])


class ClaimUsernameRequest(BaseModel):
    username: str = Field(..., description="Desired handle. Lowercased + validated server-side.")


class SellerUsernameState(BaseModel):
    username: str
    last_changed_at: str | None = None
    cooldown_ends_at: str | None = None


@router.get("/me", response_model=SellerUsernameState)
async def get_my_seller_username(
    auth: Annotated[AuthContext, Depends(get_current_user)],
) -> SellerUsernameState:
    """Return the caller's claimed seller handle + cooldown state.

    Returns 404 when the caller has never claimed a handle -- the
    frontend uses that to decide whether to render the ClaimUsernameStep
    in the publish wizard.
    """
    state = await seller_identity.get_for_user(auth.user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="seller_username_not_set")
    return SellerUsernameState(**state)


@router.post("/me/username", response_model=SellerUsernameState)
async def claim_my_seller_username(
    payload: ClaimUsernameRequest,
    auth: Annotated[AuthContext, Depends(get_current_user)],
) -> SellerUsernameState:
    """Claim ``payload.username`` for the caller.

    Maps ``SellerUsernameError`` codes to structured HTTP errors:

      * ``invalid_format`` -> 400, ``{error: "invalid_format"}``
      * ``reserved``       -> 400, ``{error: "reserved"}``
      * ``taken``          -> 409, ``{error: "taken"}``
      * ``cooldown_active``-> 409, ``{error: "cooldown_active",
                                       cooldown_ends_at: "<ISO>"}``

    The frontend's PublishWizard + Settings page switch on these codes
    to render inline error copy.
    """
    try:
        state = await seller_identity.claim_username(
            user_id=auth.user_id,
            username=payload.username,
        )
    except seller_identity.SellerUsernameError as exc:
        if exc.code == "taken":
            raise HTTPException(status_code=409, detail={"error": "taken"})
        if exc.code == "cooldown_active":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "cooldown_active",
                    "cooldown_ends_at": exc.extra.get("cooldown_ends_at"),
                },
            )
        # invalid_format + reserved -- 400.
        raise HTTPException(status_code=400, detail={"error": exc.code})

    return SellerUsernameState(**state)
