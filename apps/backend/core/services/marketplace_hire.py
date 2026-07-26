"""Marketplace hires that route through Paperclip's approval surface.

A headless Friday cannot hire. She can only ask. This module turns that ask
into a Paperclip ``hire_agent`` approval linked to the issue that prompted it,
which surfaces in the Teams Inbox "Needs Attention" band
(``InboxPage`` -> ``NeedsAttentionBand`` -> ``ApprovalRow``). The roster
changes only when the owner approves.

Split of responsibility on approve, which is the thing to keep straight:

* **Paperclip** creates the ROSTER entry (``services/approvals.ts``, the
  ``hire_agent`` branch) and applies any budget policy.
* **Isol8** deploys the marketplace ARTIFACT into the container — the EFS
  workspace plus the ``openclaw.json`` merge. Paperclip knows nothing about
  either.

Both halves run on the approving user's own authenticated request
(``routers/teams/approvals.py``), so there is no privileged write path and no
event plumbing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Marker on the approval payload's metadata that identifies a hire as a
# marketplace deploy (rather than a hand-rolled agent someone created in
# Teams). Its presence is what arms the post-approve artifact deploy, so every
# non-marketplace approval flows through the approve handler untouched.
LISTING_SLUG_KEY = "isol8_listing_slug"

_TITLE_MAX = 120


def _gateway_url(env: str) -> str:
    return f"wss://ws-{env}.isol8.co"


def build_hire_approval_payload(*, listing: dict, env: str) -> dict:
    """Payload for a Paperclip ``hire_agent`` approval.

    ``adapterType`` is set EXPLICITLY and must stay that way. Paperclip's
    approve path does ``String(payload.adapterType ?? "process")``; an agent
    created as ``"process"`` is unreachable over the openclaw gateway and
    fails silently at its first wake, long after the approval looked like it
    succeeded.

    ``agentId`` is deliberately ABSENT so Paperclip CREATES the agent on
    approve instead of activating a pre-created one — nothing should exist in
    the owner's roster before they say yes.
    """
    tags = listing.get("tags") or []
    return {
        "name": listing.get("name", ""),
        "role": "general",
        "title": (listing.get("summary") or "")[:_TITLE_MAX],
        "capabilities": ", ".join(tags),
        "adapterType": "openclaw_gateway",
        "adapterConfig": {"url": _gateway_url(env)},
        "budgetMonthlyCents": 0,
        "metadata": {LISTING_SLUG_KEY: listing.get("slug", "")},
    }


def listing_slug_of(payload: dict | None) -> str | None:
    """The marketplace slug on an approval payload, or None if it is not a
    marketplace hire.

    Returning None for everything else is what keeps the post-approve deploy
    hook invisible to the existing approve flow.
    """
    slug = ((payload or {}).get("metadata") or {}).get(LISTING_SLUG_KEY)
    return slug or None


async def create_hire_approval(*, owner_id: str, issue_id: str, listing: dict) -> str | None:
    """Create a PENDING ``hire_agent`` approval linked to ``issue_id``.

    Creating it changes nothing on its own. ``issueIds`` is what makes the
    request show up ON the task that prompted it (upstream links them via
    ``issueApprovalsSvc.linkManyForApproval``) rather than only in a global
    list the owner has no reason to open.

    Returns the approval id, or None when the owner has no Paperclip company —
    a degraded state a recommendation must not raise on, since the listings
    themselves are still worth returning.
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id is None:
            logger.info("create_hire_approval: no paperclip company for owner=%s", owner_id)
            return None
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        created = await admin.create_approval(
            session_cookie=cookie,
            company_id=company_id,
            body={
                "type": "hire_agent",
                "payload": build_hire_approval_payload(listing=listing, env=settings.ENVIRONMENT),
                "issueIds": [issue_id],
            },
        )
        return created.get("id")
    finally:
        await http.aclose()
