"""The workspace's display name: the META ``name``, then the owner id.

Used for the Stripe account name, the agent mail From name and footer, and the team-address slug.
Never empty, so a workspace whose name was never set still gets an account and an address.
"""

from __future__ import annotations

from core.repositories.workspace_repo import WorkspaceRepo


async def saved_name(owner_id: str) -> str | None:
    """META ``name``, else None."""
    ws = await WorkspaceRepo().get_workspace(owner_id)
    return ws.name.strip() if ws and (ws.name or "").strip() else None


async def workspace_name(owner_id: str) -> str:
    return await saved_name(owner_id) or owner_id
