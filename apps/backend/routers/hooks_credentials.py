"""Clerk-authenticated read of the calling user's hook credentials.

Backs the Settings → Webhooks panel. Returns the public URL + token +
last-rotated timestamp. Unlike ``hooks_proxy.py``, this route DOES use
``get_current_user`` — it's the user's own browser asking for their own
credentials.

Owner resolution: the ``containers`` row is keyed on ``owner_id``,
which is ``org_id`` for org users and ``user_id`` for personal users.
We use ``resolve_owner_id(auth)`` from ``core.auth`` to handle both cases.
"""

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import container_repo

router = APIRouter()


@router.get("/credentials")
async def get_credentials(user: AuthContext = Depends(get_current_user)) -> dict:
    """Return the calling user's webhook credentials for the Settings UI.

    Clerk-authenticated. Returns ``{public_url, token, rotated_at}`` or
    404 if the user has no provisioned container yet.
    """
    owner_id = resolve_owner_id(user)
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="container_not_provisioned")
    return {
        "public_url": f"{settings.PUBLIC_API_BASE}/api/v1/hooks/u/{owner_id}/",
        "token": container.get("hook_token") or "",
        "rotated_at": container.get("hook_token_rotated_at"),
    }
