"""Marketplace seller profile endpoints.

Sellers pick a public handle that anchors their identity on listings and
(future) star / credibility surfaces. The publish flow gates on a profile
existing — see ``routers/marketplace_listings.create_listing`` for the
``seller_profile_required`` 409.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import AuthContext, get_current_user
from core.services import seller_profile
from schemas import marketplace as schemas


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace/seller", tags=["marketplace"])


@router.get("/me", response_model=schemas.SellerProfile)
async def get_my_profile(
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Return the caller's seller profile, or 404 if none yet."""
    profile = await seller_profile.get_profile(auth.user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="seller_profile_required")
    return profile


@router.put("/me", response_model=schemas.SellerProfile)
async def upsert_my_profile(
    payload: schemas.SellerProfileWrite,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    """Create or update the caller's seller profile."""
    try:
        return await seller_profile.upsert_profile(
            seller_id=auth.user_id,
            handle=payload.handle,
            display_name=payload.display_name,
            bio=payload.bio,
        )
    except seller_profile.HandleFormatError:
        raise HTTPException(status_code=400, detail="handle_format")
    except seller_profile.HandleReservedError:
        raise HTTPException(status_code=409, detail="handle_reserved")
    except seller_profile.HandleTakenError:
        raise HTTPException(status_code=409, detail="handle_taken")
    except seller_profile.SellerProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/check-handle", response_model=schemas.HandleAvailability)
async def check_handle(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    h: str = Query(..., description="Candidate handle to check, case-insensitive."),
):
    """Pre-flight check for the handle picker.

    Returns ``{available: true}`` if the handle would be accepted by
    ``PUT /seller/me`` for the caller — including the case where the
    caller already owns the handle. Otherwise returns a ``reason`` the
    UI can render inline (``format`` / ``reserved`` / ``taken``).
    """
    normalised = seller_profile.normalize_handle(h)
    try:
        seller_profile.validate_handle(normalised)
    except seller_profile.HandleFormatError:
        return {"available": False, "reason": "format"}
    except seller_profile.HandleReservedError:
        return {"available": False, "reason": "reserved"}

    existing = await seller_profile.get_profile_by_handle(normalised)
    if existing is None:
        return {"available": True, "reason": None}
    if existing.get("seller_id") == auth.user_id:
        return {"available": True, "reason": None}
    return {"available": False, "reason": "taken"}


@router.get("/handle/{handle}", response_model=schemas.SellerProfilePublic)
async def get_profile_by_handle_public(handle: str):
    """Public profile lookup by handle.

    No auth — surfaces handle, display_name, bio, created_at for
    rendering on a future ``/marketplace/seller/{handle}`` page. The
    ``seller_id`` (Clerk identifier) is intentionally omitted.
    """
    profile = await seller_profile.get_profile_by_handle(handle)
    if not profile:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "handle": profile["handle"],
        "display_name": profile.get("display_name"),
        "bio": profile.get("bio"),
        "created_at": profile.get("created_at", ""),
    }
