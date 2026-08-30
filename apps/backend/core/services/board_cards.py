"""File one approval card on an owner's Paperclip board, de-duped.

Both container-authed card rails do the identical five steps — company-id gate,
admin cookie, list pending, de-dupe on our own payload marker, create — and
differ only in the payload they build and (for P0a) a billing pre-check in
front. That sequence lives here once.

Paperclip validates ``type`` against its own four-value enum, so every card we
file rides ``request_board_approval`` and ``payload.isol8_action`` is what makes
it ours — both for de-dup here and for the row action in the UI.

De-dup is on PENDING cards only: ``list_approvals`` is documented as "List
pending approvals" (paperclip_admin_client.py:1152), so the backend never tries
to infer "already approved" from it. Friday reads a card's outcome from her own
Paperclip surface, which is where approvals live.
"""

from __future__ import annotations

import logging
from typing import Callable

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_stripe import resolve_company_name
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "request_board_approval"

_PENDING = ("pending", "revision_requested")


def _matches(approval: dict, marker: str, extra: dict | None = None) -> bool:
    if approval.get("type") != APPROVAL_TYPE or approval.get("status") not in _PENDING:
        return False
    payload = approval.get("payload") or {}
    if payload.get("isol8_action") != marker:
        return False
    return all(payload.get(k) == v for k, v in (extra or {}).items())


async def file_board_card(
    owner_id: str,
    *,
    marker: str,
    build_payload: Callable[[str], dict],
    issue_id: str | None = None,
    dedupe: dict | None = None,
) -> str | None:
    """The id of the card — newly created, or the pending one it deduped onto.
    ``None`` when the owner has no Paperclip company yet.

    ``build_payload(company_name) -> dict`` is called only once we know a card
    actually needs filing — after the company-id gate AND the dedupe check — so
    a no-company or already-pending owner never triggers the extra
    ``resolve_company_name`` lookup (a naive "resolve the name up front"
    ordering was tried and made the no-company path issue a live, unmocked
    Paperclip company lookup).
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        logger.info("board_cards: no Paperclip company for owner_id=%s marker=%s", owner_id, marker)
        return None
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    existing = await admin.list_approvals(session_cookie=cookie, company_id=company_id)
    approvals = existing if isinstance(existing, list) else (existing or {}).get("approvals") or []
    for approval in approvals:
        if _matches(approval, marker, dedupe):
            return approval.get("id")
    company_name = await resolve_company_name(owner_id)
    body = {
        "type": APPROVAL_TYPE,
        "payload": {"isol8_action": marker, **build_payload(company_name)},
        "issueIds": [issue_id] if issue_id else [],
    }
    created = await admin.create_approval(session_cookie=cookie, company_id=company_id, body=body)
    return created.get("id")
