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
    # Ensure the users row exists first. `set_push_token` no-ops on an absent
    # row (its scoped UpdateItem is conditional on `attribute_exists(user_id)`),
    # and a brand-new user can POST /devices before the Clerk `user.created`
    # webhook has written their row — which would store nothing yet return a
    # false "registered". The client then caches that as success and never
    # retries, so the user would silently receive no pushes. Creating the row
    # here (idempotent; mirrors POST /users/sync) makes the write land.
    if not await user_repo.get(auth.user_id):
        await user_repo.put(auth.user_id, auth.email)
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
