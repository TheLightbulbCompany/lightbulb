"""Idempotent Paperclip workspace provisioning helper.

Centralizes the (resolve email -> get_paperclip_provisioning ->
provision_org -> close httpx) chain so every entry point that needs
to ensure a personal user has a Paperclip workspace can call the
same function. Failures are swallowed and logged — Paperclip outage
must never break the calling code path (container provisioning,
user sync, /teams BFF).

Personal context only. Org context is owned by Clerk's
``organization.created`` webhook in ``routers/webhooks.py``; calling
this for an org owner would create a personal-shaped row and race
the webhook.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def ensure_paperclip_workspace(*, owner_id: str, clerk_user_id: str) -> None:
    """Provision the Paperclip company for this owner if it doesn't exist.

    Personal context: ``owner_id == clerk_user_id`` and we pass the
    user's own id as the Paperclip provisioning ``owner_id``. Org
    context: skipped — the Clerk ``organization.created`` webhook
    owns it.

    Idempotent: ``PaperclipProvisioning.provision_org`` short-circuits on
    an existing ``status="active"`` row. Safe to call from /users/sync,
    /container/provision, and the /teams BFF.
    """
    if owner_id != clerk_user_id:
        return
    try:
        from core.services.paperclip_owner_email import lookup_owner_email
        from routers.webhooks import _close_paperclip_http, _get_paperclip_provisioning

        owner_email = await lookup_owner_email(org_id=None, fallback_user_id=clerk_user_id)
        if not owner_email:
            logger.warning(
                "paperclip auto-provision skipped: no email for user %s",
                clerk_user_id,
            )
            return
        provisioning = await _get_paperclip_provisioning()
        try:
            await provisioning.provision_org(
                owner_id=clerk_user_id,
                owner_user_id=clerk_user_id,
                owner_email=owner_email,
            )
        finally:
            await _close_paperclip_http(provisioning)
    except Exception:
        logger.exception(
            "Paperclip auto-provision failed for user %s — user can retry from /teams",
            clerk_user_id,
        )
