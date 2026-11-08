"""The workspace's display name: the META ``name``, then the Clerk org name, then the owner id.

Used for the Stripe account name, the agent mail From name and footer, and the team-address slug.
Never empty, so a workspace whose name was never set still gets an account and an address.
"""

from __future__ import annotations

from core.auth import is_org_owner_id
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import clerk_admin


async def saved_name(owner_id: str) -> str | None:
    """META ``name``, else the Clerk org name, else None."""
    ws = await WorkspaceRepo().get_workspace(owner_id)
    if ws and (ws.name or "").strip():
        return ws.name.strip()
    org = await clerk_admin.get_organization(owner_id) if is_org_owner_id(owner_id) else None
    return ((org or {}).get("name") or "").strip() or None


async def workspace_name(owner_id: str) -> str:
    return await saved_name(owner_id) or owner_id
