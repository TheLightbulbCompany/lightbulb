"""Owner-email resolver shared between Clerk webhooks and the
Paperclip retry worker.

Originally lived in ``routers/webhooks.py`` as ``_lookup_owner_email``
but the retry worker (``core/services/update_service.py``) needs the
same Clerk-fallback behaviour to avoid trusting a stale ``owner_email``
cached in the retry payload. Routers can't be safely imported from
services (back-edge through ``update_service.PAPERCLIP_RETRY_KIND``
already creates a forward edge from webhooks to update_service), so
the helper lives here as the single source of truth.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def lookup_owner_email(*, org_id: Optional[str], fallback_user_id: Optional[str]) -> Optional[str]:
    """Pull the org owner's email so ``provision_member`` can sign them in.

    For ``organizationMembership.created`` Clerk's payload includes
    ``data.organization.created_by`` (the owner's user_id), but NOT the
    owner's email. We read it from the ``users`` repo where the
    ``user.created`` webhook persisted it.

    Fallback to Clerk Backend API (``clerk_admin.get_user``) when the
    repo row is missing or lacks an email. This catches two real
    cases that the retry worker can't otherwise recover from:

      * a prior ``user.created`` webhook persistence failed, leaving
        no row at all, and
      * older rows that predate the email field on ``users``.

    Without the Clerk fallback the resolver returns None forever and
    member onboarding stays permanently ``pending``. Returns None
    (not raises) on Clerk failure so the retry worker can try again
    next cycle without crashing the webhook.

    ``org_id`` is optional and only used for log context — pass it
    when known (webhook handlers) or ``None`` when not (retry worker
    payloads omit org_id).
    """
    if not fallback_user_id:
        return None
    from core.repositories import user_repo

    # Fast path: users repo already has the email (populated by the
    # ``user.created`` webhook).
    try:
        row = await user_repo.get(fallback_user_id)
        if row and row.get("email"):
            return row["email"]
    except Exception:
        logger.exception("owner email lookup (user_repo) failed for org=%s", org_id)

    # Fallback: ask Clerk directly. Mirrors the email-extraction
    # pattern in ``routers/teams/agents.py:_resolve_user_email``
    # (primary_email_address_id → first email → None).
    try:
        from core.services import clerk_admin

        user = await clerk_admin.get_user(fallback_user_id)
        if not user:
            return None
        primary_id = user.get("primary_email_address_id")
        addresses = user.get("email_addresses") or []
        if primary_id:
            for entry in addresses:
                if isinstance(entry, dict) and entry.get("id") == primary_id:
                    addr = entry.get("email_address")
                    if addr:
                        return addr
        # Fall back to the first email if the primary id pointer is unset.
        for entry in addresses:
            if isinstance(entry, dict):
                addr = entry.get("email_address")
                if addr:
                    return addr
    except Exception:
        logger.exception("owner email lookup (clerk_admin) failed for org=%s", org_id)

    return None
