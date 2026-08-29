"""Friday asks the owner to set up the company's Stripe account.

Files a Paperclip approval so it lands in the Teams inbox (and on the issue
that needed it). Paperclip validates ``type`` against its own four-value enum,
so the card rides ``request_board_approval`` and ``payload.isol8_action`` is
what makes it ours — both for de-dup here and for the row action in the UI.
The card is informational — the owner acts on it from the row's "Set up
Stripe" button, which calls ``POST /company/stripe/create`` and then approves
the request. Never a gate: if the owner ignores it, chat keeps working.
"""

from __future__ import annotations

import logging

from core.repositories import billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_stripe import resolve_company_name
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

# Paperclip's enum: hire_agent | approve_ceo_strategy |
# budget_override_required | request_board_approval. Anything else is a 400.
APPROVAL_TYPE = "request_board_approval"
ACTION_MARKER = "company_stripe"


def _is_pending_company_stripe(approval: dict) -> bool:
    return (
        approval.get("type") == APPROVAL_TYPE
        and approval.get("status") in ("pending", "revision_requested")
        and (approval.get("payload") or {}).get("isol8_action") == ACTION_MARKER
    )


async def request_company_stripe(*, owner_id: str, issue_id: str | None, reason: str) -> str | None:
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        return None
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        logger.info("company_stripe_approval: no Paperclip company for owner_id=%s", owner_id)
        return None
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    existing = await admin.list_approvals(session_cookie=cookie, company_id=company_id)
    approvals = existing if isinstance(existing, list) else (existing or {}).get("approvals") or []
    for approval in approvals:
        if _is_pending_company_stripe(approval):
            return approval.get("id")
    company_name = await resolve_company_name(owner_id)
    body = {
        "type": APPROVAL_TYPE,
        "payload": {
            "isol8_action": ACTION_MARKER,
            "title": f"Set up {company_name} on Stripe",
            "reason": (reason or "")[:500],
        },
        "issueIds": [issue_id] if issue_id else [],
    }
    created = await admin.create_approval(session_cookie=cookie, company_id=company_id, body=body)
    return created.get("id")
