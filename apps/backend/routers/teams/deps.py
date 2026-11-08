"""Shared FastAPI deps for the Teams BFF routers.

Centralizes the (auth -> workspace row -> membership) check that every
Teams endpoint behind ``_ctx`` runs at the top of its handler. Lives
as a class + a resolver so we can unit-test without a FastAPI request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id
from core.repositories.workspace_repo import Workspace


def envelope_list(upstream: Any, key: str) -> dict[str, list[dict]]:
    """Wrap an upstream list response in a ``{key: [...]}`` envelope.

    Some upstream reads return bare JSON arrays. The Teams frontend
    panels read ``data?.<resource>`` (envelope shape) so without
    server-side wrapping each panel silently renders empty.

    Tolerates either upstream shape — bare list, or a dict with the
    expected key / a generic ``items`` key — so a future upstream
    change to envelope shape won't 5xx the panel.

    Drops non-dict rows (defensive: some upstream paths can splice in
    error markers).
    """
    if isinstance(upstream, list):
        rows = upstream
    elif isinstance(upstream, dict):
        rows = upstream.get(key) or upstream.get("items") or []
    else:
        rows = []
    return {key: [row for row in rows if isinstance(row, dict)]}


class TeamsContextError(HTTPException):
    """HTTPException with a friendlier name for raising at the boundary."""


@dataclass
class TeamsContext:
    user_id: str
    org_id: str | None
    owner_id: str


class _Repo(Protocol):
    async def get_workspace(self, org_id: str) -> Workspace | None: ...


# Reconcile drives provisioning for a visitor who is not a member yet.
# Returns None when provisioning was (re)launched and the UI should keep
# polling (202); returns a blocked-gate payload dict when a provision gate is
# up (no subscription / credits / provider) so the caller can surface 402
# "subscribe required" instead of an infinite provisioning spinner.
Reconcile = Callable[[], Awaitable[Optional[dict]]]


async def resolve_teams_context(
    *,
    auth: AuthContext,
    repo: _Repo,
    reconcile: Optional[Reconcile] = None,
) -> TeamsContext:
    """Build a ``TeamsContext`` for the current request, or raise.

    Status codes follow spec §9 error handling:
      - 202 while the workspace is still provisioning, and — with
        ``reconcile`` — when this human is not in the workspace row's
        ``member_user_ids`` yet (the reconcile joins them).
      - 503 if the workspace is ``failed`` or otherwise not active.
      - 409 when not joined and no ``reconcile`` is wired.

    **Self-healing provisioning.** ``/teams`` never polled
    ``GET /container/status``, so the caller passes a ``reconcile`` coroutine
    factory; for a non-member we fire it (idempotent + gate-first) and return
    **202** so the frontend keeps polling until the member set includes them.
    Pure unit tests that don't supply it keep the 409 (no side effect).

    Membership is the workspace META row's member set only; there is no
    other identity or session behind it.
    """
    owner_id = resolve_owner_id(auth)
    ws = await repo.get_workspace(owner_id)
    # "seeding" is the pre-side-effect claim provisioning writes: same "keep
    # polling" verdict as a mid-chain "provisioning" workspace.
    if ws is not None and ws.status in ("seeding", "provisioning"):
        raise TeamsContextError(status_code=202, detail="team workspace provisioning")
    if ws is not None and ws.status == "failed":
        raise TeamsContextError(status_code=503, detail="team workspace provisioning failed")
    if ws is not None and ws.status != "active":
        raise TeamsContextError(status_code=503, detail=f"team workspace status={ws.status}")
    if ws is None or auth.user_id not in ws.member_user_ids:
        if reconcile is not None:
            blocked = await reconcile()
            if blocked is not None:
                raise TeamsContextError(status_code=402, detail=blocked)
            raise TeamsContextError(status_code=202, detail="team workspace provisioning")
        raise TeamsContextError(status_code=409, detail="team workspace not provisioned")

    return TeamsContext(user_id=auth.user_id, org_id=auth.org_id, owner_id=owner_id)
