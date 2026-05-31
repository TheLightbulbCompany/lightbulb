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

    service = KeyService()
    deleted = await service.delete_key(owner_id, tool_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")

    return {"status": "ok", "tool_id": tool_id}
