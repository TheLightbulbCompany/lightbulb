"""Router for BYOK API key management."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services.key_service import (
    SUPPORTED_LLM_PROVIDERS,
    KeyService,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Only LLM provider keys (OpenAI/Anthropic) are accepted; the service layer
# validates the key and mirrors it into Secrets Manager.
ALLOWED_KEY_IDS = set(SUPPORTED_LLM_PROVIDERS.keys())


class SetKeyRequest(BaseModel):
    api_key: str


@router.get("")
async def list_keys(
    auth: AuthContext = Depends(get_current_user),
):
    """List configured API keys (no values exposed)."""
    owner_id = resolve_owner_id(auth)
    service = KeyService()
    keys = await service.list_keys(owner_id)
    return {
        "keys": keys,
        "supported_llm_providers": list(SUPPORTED_LLM_PROVIDERS.keys()),
    }


async def _redeploy_if_key_is_live(owner_id: str, tool_id: str) -> bool:
    """Roll the container when the key just written is the one it is running on.

    ECS resolves a task's ``secrets:`` at task START and injects the value as
    an environment variable. A live process's environment cannot be mutated
    from outside, so a rotated key reaches the container only on a fresh task
    — without this, the owner saves a new key, sees no error, and keeps
    running on the old one indefinitely.

    Deliberately narrow. ``_build_byo_secrets_for_user`` emits a SINGLE
    ``secrets:`` entry, for the active ``byo_provider``, so rotating the OTHER
    provider's key (pre-staging a switch) or saving any key while on
    chatgpt_oauth/bedrock changes nothing a running task holds — restarting
    there would be downtime for no effect. A provider SWITCH is already cold
    (``billing_service.change_provider`` reprovisions on any BYOK side), so it
    must not be handled here too, or the switch would restart twice.

    Returns whether a redeploy was actually issued, so the caller can tell the
    user their container is restarting.
    """
    from core.containers import get_ecs_manager
    from core.services import container_provision

    # Entirely best-effort: the key IS already stored by the time we get here.
    # Neither the provider lookup (DynamoDB) nor the redeploy (ECS) may fail
    # the write, or the owner retries a save that already succeeded. The next
    # task start picks the new value up regardless, so the worst case is the
    # pre-existing behaviour.
    try:
        provider_choice, byo_provider = await container_provision.resolve_provider_choice(owner_id)
        if provider_choice != "byo_key" or byo_provider != tool_id:
            return False
        await get_ecs_manager().force_redeploy_user_service(owner_id)
    except Exception:
        logger.exception("byo-key rotation: redeploy skipped for owner %s", owner_id)
        return False
    return True


@router.put("/{tool_id}")
async def set_key(
    tool_id: str,
    body: SetKeyRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Store an API key for a tool."""
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    if tool_id not in ALLOWED_KEY_IDS:
        raise HTTPException(status_code=400, detail=f"Unsupported tool: {tool_id}")

    service = KeyService()
    await service.set_key(owner_id, tool_id, body.api_key)
    return {
        "status": "ok",
        "tool_id": tool_id,
        "container_restarting": await _redeploy_if_key_is_live(owner_id, tool_id),
    }


@router.delete("/{tool_id}")
async def delete_key(
    tool_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Remove an API key. The tool becomes unavailable until a new key is added."""
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    if tool_id not in ALLOWED_KEY_IDS:
        raise HTTPException(status_code=400, detail=f"Unsupported tool: {tool_id}")

    service = KeyService()
    deleted = await service.delete_key(owner_id, tool_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")

    return {"status": "ok", "tool_id": tool_id}
