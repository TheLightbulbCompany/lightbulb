"""Shared FastAPI deps for the Teams BFF routers.

Centralizes the (auth -> paperclip-companies row -> user-session cookie)
chain that every Teams endpoint runs at the top of its handler. Lives
as a class + a resolver so we can unit-test without a FastAPI request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id
from core.repositories.paperclip_repo import PaperclipCompany


def envelope_list(upstream: Any, key: str) -> dict[str, list[dict]]:
    """Wrap an upstream list response in a ``{key: [...]}`` envelope.

    Upstream Paperclip's ``GET /api/companies/:co/<resource>`` endpoints
    return bare JSON arrays (``res.json(result)``). The Teams frontend
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
    company_id: str
    paperclip_user_id: str
    session_cookie: str


class _Repo(Protocol):
    async def get(self, user_id: str) -> PaperclipCompany | None: ...


SessionFactory = Callable[[str], Awaitable[str]]


async def resolve_teams_context(
    *,
    auth: AuthContext,
    repo: _Repo,
    session_factory: SessionFactory,
) -> TeamsContext:
    """Build a ``TeamsContext`` for the current request, or raise.

    Status codes follow spec §9 error handling:
      - 409 if there's no DDB row at all (provisioning never started).
      - 202 if the row exists but ``status != "active"``. The UI polls.
      - 503 if the row is ``status="failed"`` (operator-resolved).
    """
    company = await repo.get(auth.user_id)
    if company is None:
        raise TeamsContextError(status_code=409, detail="team workspace not provisioned")
    if company.status == "provisioning":
        raise TeamsContextError(status_code=202, detail="team workspace provisioning")
    if company.status == "failed":
        raise TeamsContextError(status_code=503, detail="team workspace provisioning failed")
    if company.status != "active":
        raise TeamsContextError(status_code=503, detail=f"team workspace status={company.status}")

    cookie = await session_factory(auth.user_id)

    return TeamsContext(
        user_id=auth.user_id,
        org_id=auth.org_id,
        owner_id=resolve_owner_id(auth),
        company_id=company.company_id,
        paperclip_user_id=company.paperclip_user_id,
        session_cookie=cookie,
    )
