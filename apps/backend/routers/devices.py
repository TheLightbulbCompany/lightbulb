"""Device (push-token) registration endpoints."""

import logging

from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.dynamodb import utc_now_iso
from core.repositories import user_repo
from schemas.user_schemas import DeviceRegisterRequest, DeviceRegisterResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=DeviceRegisterResponse, operation_id="register_device")
async def register_device(
    body: DeviceRegisterRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Register (or refresh) this device's Expo push token for the caller."""
    entry = {"token": body.token, "platform": body.platform, "updatedAt": utc_now_iso()}
    await user_repo.set_push_token(auth.user_id, body.deviceId, entry)
    return {"status": "registered"}


@router.delete("/{device_id}", operation_id="unregister_device")
async def unregister_device(
    device_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Remove this device's push token (sign-out)."""
    await user_repo.clear_push_token(auth.user_id, device_id)
    return {"status": "cleared"}
