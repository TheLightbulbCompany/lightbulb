"""A person's own agent keys for the hosted MCP (Settings > Agent keys). Never exposes a hash."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import agent_keys, clerk_admin

router = APIRouter()


class CreateAgentKey(BaseModel):
    name: str
    # Default 90 days; null keeps the key until it is revoked.
    expires_in_days: int | None = agent_keys.DEFAULT_EXPIRY_DAYS


async def require_live_member(auth: AuthContext) -> str:
    """The caller's workspace, checked with Clerk now: a session JWT can outlive removal (the same
    check the verifier makes). Fails closed. Webhook agents use it too."""
    owner_id = resolve_owner_id(auth)
    try:
        member = any(
            o["id"] == owner_id for o in await clerk_admin.list_user_organizations(auth.user_id, limit=100, strict=True)
        )
    except Exception:
        member = False
    if not member:
        raise HTTPException(status_code=403, detail="You are not a member of this workspace.")
    return owner_id


def _owner(auth: AuthContext) -> str:
    """A free account's keys are its own (owner = the user): they reach the network tools only."""
    return auth.org_id or auth.user_id


@router.get("")
async def list_agent_keys(auth: AuthContext = Depends(get_current_user)):
    return {"keys": await agent_keys.list_keys(_owner(auth), auth.user_id)}


@router.post("")
async def create_agent_key(body: CreateAgentKey, auth: AuthContext = Depends(get_current_user)):
    if body.expires_in_days is not None and not 1 <= body.expires_in_days <= 365:
        raise HTTPException(status_code=400, detail="Expiry must be 1-365 days, or none.")
    owner_id = await require_live_member(auth) if auth.org_id else auth.user_id
    try:
        return await agent_keys.create_key(owner_id, auth.user_id, body.name, body.expires_in_days)
    except agent_keys.AgentKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{key_id}")
async def revoke_agent_key(key_id: str, auth: AuthContext = Depends(get_current_user)):
    if not await agent_keys.revoke_key(_owner(auth), auth.user_id, key_id):
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "ok"}
