"""Workspace email invitations — send / list pending / revoke.

Back since 2026-10-08, NEXT TO the join link (``workspace_join``), not instead
of it. Email invitations were removed on 2026-09-10 together with per-seat
billing, because an email+role invite modelled a seat grant; the join link
replaced them on 09-14. Billing is per workspace now, so that reason is gone,
and "invite my cofounder by email" is where people look first (Slack's shape:
a link to share AND invitations by email).

Clerk owns the invitation: it stores it, emails it, and its link lands on our
``/sign-up``, where the prebuilt ``<SignUp>`` consumes the ticket and creates
the membership. From there the existing ``organizationMembership.created``
webhook does exactly what it does for a join link. Nothing is stored here.

Everyone is invited as ``org:member`` — the same rule as the join link.

**The invitee's email is PII.** It reaches Clerk and the admin's own response,
and nothing else: no log line, metric dimension or product-event property.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException

from core.config import settings
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.services import clerk_admin

logger = logging.getLogger(__name__)


def _iso(ms: int | None) -> str | None:
    """Clerk timestamps are epoch milliseconds; the API speaks ISO 8601 UTC."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _view(inv: dict) -> dict:
    return {
        "id": inv.get("id") or "",
        "email": (inv.get("email_address") or "").lower(),
        "created_at": _iso(inv.get("created_at") or 0),
        "expires_at": _iso(inv.get("expires_at")),
        "status": "pending",
    }


async def list_pending(org_id: str) -> list[dict]:
    """Pending invitations, newest first."""
    rows = await clerk_admin.list_pending_organization_invitations(org_id)
    rows = sorted(rows, key=lambda r: r.get("created_at") or 0, reverse=True)
    return [_view(r) for r in rows]


async def send(org_id: str, email: str, *, inviter: str) -> dict:
    """Invite ``email`` to ``org_id``. 409 if they are a member or already invited.

    Both checks run HERE, before Clerk, because Clerk does not document the
    codes it answers a duplicate with and the UI needs the two cases told
    apart. ``create_organization_invitation`` maps Clerk's own conflict codes
    to the same 409s as a fallback (a member matched by a secondary email, or
    a race between two admins).
    """
    email = email.strip().lower()
    try:
        members = await clerk_admin.list_organization_members(org_id, strict=True)
    except Exception as e:  # noqa: BLE001 — a Clerk blip must not read as "not a member"
        logger.warning("orgs.invitation.member_check_failed org_id=%s err=%s", org_id, type(e).__name__)
        raise HTTPException(status_code=502, detail=clerk_admin.INVITATIONS_UNAVAILABLE) from e
    if any((m.get("email") or "").lower() == email for m in members):
        raise HTTPException(status_code=409, detail=clerk_admin.ALREADY_MEMBER)
    pending = await clerk_admin.list_pending_organization_invitations(org_id)
    if any((p.get("email_address") or "").lower() == email for p in pending):
        raise HTTPException(status_code=409, detail=clerk_admin.ALREADY_INVITED)

    inv = await clerk_admin.create_organization_invitation(
        org_id=org_id,
        email=email,
        inviter_user_id=inviter,
        redirect_url=f"{settings.FRONTEND_URL.rstrip('/')}/sign-up",
    )
    put_metric("orgs.invitation.sent")
    logger.info("orgs.invitation.sent org_id=%s inviter=%s", org_id, inviter)
    capture_product_event(distinct_id=inviter, event="invitation_sent", properties={"workspace_id": org_id})
    view = _view(inv)
    view["email"] = view["email"] or email
    return view


async def revoke(org_id: str, invitation_id: str, *, requester: str) -> None:
    """Revoke a pending invitation. Idempotent — see ``revoke_organization_invitation``."""
    await clerk_admin.revoke_organization_invitation(
        org_id=org_id, invitation_id=invitation_id, requesting_user_id=requester
    )
    put_metric("orgs.invitation.revoked")
    logger.info("orgs.invitation.revoked org_id=%s requester=%s", org_id, requester)
    capture_product_event(distinct_id=requester, event="invitation_revoked", properties={"workspace_id": org_id})
