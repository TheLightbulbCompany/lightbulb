"""Per-agent Google-account assignment API.

One Google provider holds many connected accounts; this surface lets an owner
bind a specific account to a specific agent so the agent acts as that identity
(``gog -a <email>``). The binding is persisted by ``google_accounts_repo`` and
projected by ``seed_agent_google_identity`` — into the agent's IDENTITY.md on
ECS, and as ``GOG_ACCOUNT`` in its Hermes profile's ``cli-env.json`` on
AgentCore. The seeder is the single place either is written, so every assign
and unassign re-runs it for the touched agent and this router needs no runtime
branch of its own.

Reads (GET) expose only emails + the assignment map (no secrets), so they are
owner-scoped but not admin-gated. Writes (assign / unassign) target the shared
org workspace, so they are gated to org admins — matching the connections
router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import google_accounts_repo
from core.services.google_identity import seed_agent_google_identity

router = APIRouter(prefix="/google", tags=["google-accounts"])


class AssignBody(BaseModel):
    agent_id: str


@router.get("/accounts")
async def list_accounts(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    return {
        "accounts": await google_accounts_repo.list_accounts(owner_id),
        "assignments": await google_accounts_repo.list_assignments(owner_id),
    }


@router.post("/accounts/{email}/assign")
async def assign_account(email: str, body: AssignBody, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    # The agent_id keys a DDB row and is handed to the IDENTITY.md seeder, so a
    # traversal-y value would persist a junk row before the workspace guard
    # rejects it — reject up front, matching the workspace-files convention.
    if "/" in body.agent_id or "\\" in body.agent_id or ".." in body.agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    owner_id = resolve_owner_id(auth)
    # An agent may only be bound to an account the owner has actually connected —
    # otherwise the seeded `gog -a <email>` instruction would name a keyring entry
    # that doesn't exist. Validate membership against the live inventory.
    accounts = await google_accounts_repo.list_accounts(owner_id)
    if not any(a["email"] == email for a in accounts):
        raise HTTPException(status_code=404, detail=f"google account not connected: {email}")
    await google_accounts_repo.set_assignment(owner_id, body.agent_id, email)
    # Project the new binding (IDENTITY.md on ECS, GOG_ACCOUNT on AgentCore) so the
    # agent acts as the right account on its next run, without a reprovision.
    # NOTE the ordering: the seeder raises (→ 503) when it cannot read which runtime this
    # owner is on, and by then the row above is stored. That is deliberate — the row is the
    # binding's truth, the projection is derived from it, and the owner's retry re-projects.
    # The window is a binding the UI lists that the agent has not received yet.
    await seed_agent_google_identity(owner_id, body.agent_id)
    return {"assignments": await google_accounts_repo.list_assignments(owner_id)}


@router.delete("/accounts/assign/{agent_id}")
async def unassign_agent(agent_id: str, auth: AuthContext = Depends(get_current_user)):
    require_org_admin(auth)
    # The agent_id keys a DDB row and is handed to the IDENTITY.md seeder, so a
    # traversal-y value would persist a junk row before the workspace guard
    # rejects it — reject up front, matching the workspace-files convention.
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    owner_id = resolve_owner_id(auth)
    await google_accounts_repo.remove_assignment(owner_id, agent_id)
    # Re-seed reflects the agent's explicit assignment only: with the binding
    # gone there is none, so the IDENTITY.md block (or GOG_ACCOUNT) is removed
    # and the agent reverts to bare `gog` (the keyring's account).
    await seed_agent_google_identity(owner_id, agent_id)
    return {"assignments": await google_accounts_repo.list_assignments(owner_id)}
