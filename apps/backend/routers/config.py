"""Config router — read + write endpoints over the owner's roster profiles.

The raw editor and the settings-panel RPC path share one seam
(``runtime_rpc._config_get_roster_map`` / ``config_patch_roster``), so they can never
disagree about what "the config" is.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import (
    AuthContext,
    get_current_user,
    require_org_admin,
    resolve_owner_id,
)
from core.services.runtime_rpc import _config_get_roster_map, config_patch_roster
from schemas.config import ConfigPatchRequest

logger = logging.getLogger(__name__)
router = APIRouter()


def _patch_touches_channels(patch: dict) -> bool:
    """Return True if the patch modifies any channels.<provider>.* fields.

    A top-level `channels` key alone is fine (e.g., the initial scaffold);
    we only care if the caller is trying to configure channel accounts,
    tokens, bindings, etc.
    """
    if not isinstance(patch, dict):
        return False
    channels = patch.get("channels")
    if not isinstance(channels, dict) or not channels:
        return False
    # Any non-empty nested dict under channels.* means an actual config
    for _provider, provider_cfg in channels.items():
        if isinstance(provider_cfg, dict) and provider_cfg:
            return True
    return False


@router.get(
    "",
    summary="Read the caller's roster config",
    description=(
        "Returns the merged config of the caller's agent roster. Org callers must be "
        "org_admin — the config carries shared secrets (future MCP env, etc.) that "
        "non-admin members shouldn't see."
    ),
)
async def get_config(
    auth: AuthContext = Depends(get_current_user),
):
    # Mirror patch_config's gate. The shared org config carries secrets
    # only the admin set up; without this, a non-admin member could read
    # MCP env via this endpoint or the Overview Configuration button even
    # though they can't manage them via PATCH /config.
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    roster_map = await _config_get_roster_map(owner_id)
    return {"owner_id": owner_id, "config": roster_map["config"], "hash": roster_map["hash"]}


@router.patch(
    "",
    summary="Patch the caller's roster config",
    description=(
        "Fans the patch out to every roster profile. Derives owner_id from the auth "
        "context (org_id if org, else user_id). Requires org_admin for org callers."
    ),
)
async def patch_config(
    body: ConfigPatchRequest,
    auth: AuthContext = Depends(get_current_user),
):
    # Org admin check (personal context passes through)
    require_org_admin(auth)

    owner_id = resolve_owner_id(auth)

    # Channels have no Hermes equivalent yet (task-9-scan.md rows #10/#11, LATER/M5) --
    # rejecting outright means a channels patch never lands as inert `channels.*` junk in
    # every roster profile's config.yaml.
    if _patch_touches_channels(body.patch):
        raise HTTPException(
            status_code=400,
            detail="INVALID_REQUEST: channels are not supported on an AgentCore workspace yet",
        )
    # M6 row #12: fan the patch out to every roster profile via the same
    # config_patch_roster the config.patch RPC intercept uses (M4-R48) -- this
    # router's ConfigPatchRequest carries no baseHash, so CAS is skipped (None).
    result = await config_patch_roster(owner_id, body.patch, None)
    return {"status": "patched", "owner_id": owner_id, "hash": result["hash"]}
