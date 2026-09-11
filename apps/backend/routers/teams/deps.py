"""Shared FastAPI deps for the Teams BFF routers.

Centralizes the (auth -> paperclip-companies row -> user-session cookie)
chain that every Teams endpoint runs at the top of its handler. Lives
as a class + a resolver so we can unit-test without a FastAPI request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from fastapi import HTTPException

from core.auth import AuthContext, resolve_owner_id
from core.repositories.paperclip_repo import PaperclipCompany
from core.services.paperclip_session_cookie import SessionRateLimited, resolve_session_cookie


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
# Reconcile drives provisioning for a visitor with no Paperclip row yet.
# Returns None when provisioning was (re)launched and the UI should keep
# polling (202); returns a blocked-gate payload dict when a provision gate is
# up (no subscription / credits / provider) so the caller can surface 402
# "subscribe required" instead of an infinite provisioning spinner.
Reconcile = Callable[[], Awaitable[Optional[dict]]]


async def resolve_teams_context(
    *,
    auth: AuthContext,
    repo: _Repo,
    session_factory: SessionFactory,
    cookies: dict[str, str],
    response,
    reconcile: Optional[Reconcile] = None,
) -> TeamsContext:
    """Build a ``TeamsContext`` for the current request, or raise.

    Status codes follow spec §9 error handling:
      - 202 if there's no DDB row yet, or the row exists but
        ``status != "active"``. The UI polls until it goes active.
      - 503 if the row is ``status="failed"`` (operator-resolved).

    **Self-healing provisioning.** ``/teams`` is a read surface — it never
    polled ``GET /container/status`` — so historically a member whose
    Paperclip row didn't exist yet was wedged on a 409 until they happened
    to open ``/chat`` (whose poll is the only thing that drove
    ``ensure_user_ready``). To make ``/teams`` reconcile like ``/chat``,
    the caller passes a ``reconcile`` coroutine factory; on a missing row we
    fire it (fire-and-forget, idempotent + gate-first) and return **202**
    "provisioning" so the frontend keeps polling, and a subsequent poll
    sees the now-active row. ``reconcile`` is optional: pure unit tests that
    don't supply it keep the old 409 (no side effect).

    ``cookies`` is the request cookie jar (``request.cookies``) and
    ``response`` is any object with ``.set_cookie(...)`` (the FastAPI
    ``Response`` in production). They are threaded through so the
    browser-held Paperclip session cookie is reused instead of minting a
    fresh sign-in on every request: ``resolve_session_cookie`` forwards
    the presented cookie when it is bound to this user, and only falls
    back to ``session_factory`` (the per-user Better Auth sign-in) on a
    cold start / expiry. The status gate runs FIRST, so a not-active
    workspace still raises 202/503 before any mint can happen — the
    provisioning fence is unchanged.
    """
    company = await repo.get(auth.user_id)
    if company is None:
        if reconcile is not None:
            # Self-heal: drive provisioning. reconcile() evaluates the provision
            # gate and, if clear, launches ensure_user_ready (idempotent +
            # gate-first) and returns None → 202 "keep polling". If a gate is
            # UP (no subscription / credits / provider) it returns the blocked
            # payload → 402 so the UI shows the subscribe/setup CTA instead of
            # spinning forever on a workspace that can never become active.
            blocked = await reconcile()
            if blocked is not None:
                raise TeamsContextError(status_code=402, detail=blocked)
            raise TeamsContextError(status_code=202, detail="team workspace provisioning")
        raise TeamsContextError(status_code=409, detail="team workspace not provisioned")
    # "seeding" is the pre-side-effect claim row provision_org's
    # cross-instance fence writes (see PaperclipRepo.try_acquire_seed_claim)
    # — same "keep polling" verdict as mid-chain "provisioning".
    if company.status in ("seeding", "provisioning"):
        raise TeamsContextError(status_code=202, detail="team workspace provisioning")
    if company.status == "failed":
        raise TeamsContextError(status_code=503, detail="team workspace provisioning failed")
    if company.status != "active":
        raise TeamsContextError(status_code=503, detail=f"team workspace status={company.status}")

    owner_id = resolve_owner_id(auth)
    if company.org_id != owner_id:
        raise TeamsContextError(status_code=403, detail="team workspace unavailable in this workspace")

    try:
        cookie = await resolve_session_cookie(
            cookies=cookies,
            response=response,
            user_id=auth.user_id,
            mint=lambda: session_factory(auth.user_id),
        )
    except SessionRateLimited as e:
        # This owner is parked because their own sign-in drew Better Auth's 429
        # (see ``paperclip_session_cookie``: the limiter keys on IP and the
        # whole backend is one). That is "come back shortly", the same verdict
        # as the provisioning waits above — so answer 503 + Retry-After instead
        # of letting the error escape as a 500.
        raise TeamsContextError(
            status_code=503,
            detail="team workspace session temporarily unavailable",
            headers={"Retry-After": str(max(1, int(e.retry_after)))},
        ) from e

    return TeamsContext(
        user_id=auth.user_id,
        org_id=auth.org_id,
        owner_id=owner_id,
        company_id=company.company_id,
        paperclip_user_id=company.paperclip_user_id,
        session_cookie=cookie,
    )
