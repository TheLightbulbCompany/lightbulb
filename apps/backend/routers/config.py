"""Config router — read + write endpoints for openclaw.json.

GET wraps read_openclaw_config_from_efs; PATCH wraps patch_openclaw_config.
Derives owner_id from the auth context, enforces org_admin for org callers
on writes, and tier-gates channel-related patches behind Starter+.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import (
    AuthContext,
    get_current_user,
    require_org_admin,
    resolve_owner_id,
)
from core.containers.config import read_openclaw_config_from_efs
from core.repositories import billing_repo
from core.services.config_patcher import (
    ConfigPatchError,
    patch_openclaw_config,
)
from core.services.provision_gate import is_subscription_active

logger = logging.getLogger(__name__)
router = APIRouter()


class ConfigPatchBody(BaseModel):
    patch: dict


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


async def _check_token_collision(owner_id: str, patch: dict) -> None:
    """Raise 409 token_already_assigned_to_other_agent if the patch introduces
    a botToken that already exists under a different accounts.<agent_id> entry
    in the owner's openclaw.json.
    """
    channels = patch.get("channels")
    if not isinstance(channels, dict):
        return

    # Collect (provider, agent_id, token) tuples in the patch
    patch_tokens: list[tuple[str, str, str]] = []
    for provider, provider_cfg in channels.items():
        if not isinstance(provider_cfg, dict):
            continue
        accounts = provider_cfg.get("accounts")
        if not isinstance(accounts, dict):
            continue
        for agent_id, account_cfg in accounts.items():
            if not isinstance(account_cfg, dict):
                continue
            token = account_cfg.get("botToken")
            if isinstance(token, str) and token.strip():
                patch_tokens.append((provider, agent_id, token.strip()))

    if not patch_tokens:
        return

    current = await read_openclaw_config_from_efs(owner_id) or {}
    current_channels = current.get("channels", {}) if isinstance(current, dict) else {}

    for provider, incoming_agent, incoming_token in patch_tokens:
        provider_cfg = current_channels.get(provider, {})
        if not isinstance(provider_cfg, dict):
            continue
        existing_accounts = provider_cfg.get("accounts", {})
        if not isinstance(existing_accounts, dict):
            continue
        for existing_agent, existing_cfg in existing_accounts.items():
            if existing_agent == incoming_agent:
                continue  # same agent, overwrite is fine
            if not isinstance(existing_cfg, dict):
                continue
            if existing_cfg.get("botToken") == incoming_token:
                raise HTTPException(
                    status_code=409,
                    detail="token_already_assigned_to_other_agent",
                )


@router.get(
    "",
    summary="Read the caller's openclaw.json config",
    description=(
        "Returns the caller's owner_id openclaw.json from EFS, or null if "
        "the file does not exist yet (e.g. mid-provisioning). Org callers "
        "resolve to org-scoped config; personal callers to user-scoped. "
        "Read access is open to any authenticated owner — no admin gate."
    ),
)
async def get_config(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    config = await read_openclaw_config_from_efs(owner_id)
    return {"owner_id": owner_id, "config": config}


@router.patch(
    "",
    summary="Patch the caller's openclaw.json config",
    description=(
        "Deep-merges the patch into the caller's owner_id openclaw.json on EFS. "
        "Derives owner_id from auth context (org_id if org, else user_id). "
        "Requires org_admin for org callers. Channel fields require an active or "
        "trialing subscription."
    ),
)
async def patch_config(
    body: ConfigPatchBody,
    auth: AuthContext = Depends(get_current_user),
):
    # Org admin check (personal context passes through)
    require_org_admin(auth)

    owner_id = resolve_owner_id(auth)

    # Subscription gate on channel fields. Channels require an actively
    # usable subscription — pre-signup users (no billing row) and
    # canceled/past_due users can't bind bots. The legacy
    # stripe_subscription_id fallback (for accounts mid-cutover) is
    # handled inside ``is_subscription_active``. Codex P1 on PR #393.
    if _patch_touches_channels(body.patch):
        account = await billing_repo.get_by_owner_id(owner_id)
        if not is_subscription_active(account):
            raise HTTPException(
                status_code=403,
                detail="channels_require_subscription",
            )

        # Bot token collision pre-check
        await _check_token_collision(owner_id, body.patch)

    try:
        await patch_openclaw_config(owner_id, body.patch)
    except ConfigPatchError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"status": "patched", "owner_id": owner_id}
