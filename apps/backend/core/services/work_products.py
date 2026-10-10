"""Register a work-product receipt on the owner's Paperclip issue, best-effort.

Creation is issue-scoped upstream (``POST /api/issues/{id}/work-products``), so
a receipt can only land when the caller knows which issue produced the work —
no issue means SKIP, never an invented one. And a receipt is bookkeeping for a
rail that already succeeded, so nothing in here may raise: every failure logs
and returns ``None``.

Company resolution + bootstrap-admin session mirror ``board_cards``.
"""

from __future__ import annotations

import logging
import re

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import ISSUE_ID_PATTERN, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)


async def register(
    owner_id: str,
    *,
    type: str,
    provider: str,
    title: str,
    issue_id: str | None = None,
    url: str | None = None,
    external_id: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """The created work-product's id, or ``None`` when the receipt was skipped
    (no issue, no Paperclip company) or failed. Never raises."""
    try:
        if not issue_id:
            logger.debug(
                "work_products: no issue_id for owner_id=%s provider=%s — skipping receipt", owner_id, provider
            )
            return None
        if not re.fullmatch(ISSUE_ID_PATTERN, issue_id):
            logger.warning("work_products: malformed issue_id for owner_id=%s — skipping receipt", owner_id)
            return None
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if not company_id:
            logger.debug("work_products: no Paperclip company for owner_id=%s — skipping receipt", owner_id)
            return None
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        # The admin session can write to ANY company's issue, so the issue must be this owner's.
        issue = await admin.get_issue(session_cookie=cookie, issue_id=issue_id)
        if (issue or {}).get("companyId") != company_id:
            logger.warning("work_products: issue %s is not owner_id=%s's — skipping receipt", issue_id, owner_id)
            return None
        # camelCase per CreateWorkProductBody's wire aliases; None-valued
        # optionals omitted so upstream applies its own defaults.
        body = {"type": type, "provider": provider, "title": title, "reviewState": "none"}
        if url is not None:
            body["url"] = url
        if external_id is not None:
            body["externalId"] = external_id
        if metadata is not None:
            body["metadata"] = metadata
        created = await admin.create_issue_work_product(session_cookie=cookie, issue_id=issue_id, body=body)
        return (created or {}).get("id")
    except Exception:
        logger.warning("work_products: receipt failed for owner_id=%s issue_id=%s", owner_id, issue_id, exc_info=True)
        return None
