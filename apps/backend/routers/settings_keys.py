"""Router for BYOK API key management."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import container_repo
from core.services.container_provision import resolve_provider_choice
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


async def _is_active_byo_key(owner_id: str, tool_id: str) -> bool:
    """True when ``tool_id`` is the key the owner's agents are running on right now."""
    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    return provider_choice == "byo_key" and (byo_provider or "anthropic") == tool_id


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

    # #1913: the plaintext is baked into every roster profile's .env at provision, so a rotated
    # active key must be re-rendered there too (write_profiles bumps .revision; the bridge
    # re-pulls on the next turn). No runtime yet => the provision reads the new key itself.
    # A key for an inactive provider is not in any profile; the provider switch reconciles.
    if await _is_active_byo_key(owner_id, tool_id):
        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        if row.get("status") == "running":  # provisioned (the shared runtime's rows carry no ARN)
            from core.services.hermes_provisioning import write_profiles

            try:
                await write_profiles(owner_id)
            except Exception:
                logger.exception("set_key: profile rewrite failed for owner=%s provider=%s", owner_id, tool_id)
                raise HTTPException(
                    status_code=502,
                    detail="Key saved, but updating your agents failed. Save it again to retry.",
                )
    return {"status": "ok", "tool_id": tool_id}


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

    # The active key lives in every agent profile and a profile with no LLM key cannot be
    # rendered, so deleting it here would silently leave agents on the "deleted" key. Switching
    # provider deletes it and rewrites the profiles.
    if await _is_active_byo_key(owner_id, tool_id):
        raise HTTPException(
            status_code=409,
            detail="This key is in use by your agents. Switch LLM provider to remove it.",
        )

    service = KeyService()
    deleted = await service.delete_key(owner_id, tool_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")

    return {"status": "ok", "tool_id": tool_id}
