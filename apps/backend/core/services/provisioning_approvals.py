"""The two cards the owner must click for the provisioning rail.

Friday runs the Stripe CLI herself; the platform's job is to put the two human
decisions in front of the owner, in the surface they already use:

  * ``stripe_cli_login`` — she ran ``stripe login --non-interactive``, which
    printed a browser URL and a verification code. The owner opens the URL,
    confirms the code, and approves the card. Friday polls and continues.
  * ``provider_consent`` — before a paid or ToS-bearing service, the owner sees
    the provider, the service, the terms link and the price, and approves.
    Only then may Friday pass ``--accept-tos``.

Paperclip validates ``type`` against its own four-value enum, so both ride
``request_board_approval`` and ``payload.isol8_action`` is what makes them ours
— both for de-dup here and for the row action in the UI (same shape as P0a's
``company_stripe_approval``).

De-dup is on PENDING cards only: ``list_approvals`` is documented as "List
pending approvals" (paperclip_admin_client.py:1152), so the backend never tries
to infer "already approved" from it. Friday reads a card's outcome from her own
Paperclip surface, which is where approvals live.
"""

from __future__ import annotations

import logging

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_stripe import resolve_company_name
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "request_board_approval"
PAIR_MARKER = "stripe_cli_login"
CONSENT_MARKER = "provider_consent"

_PENDING = ("pending", "revision_requested")


def _matches(approval: dict, marker: str, extra: dict | None = None) -> bool:
    if approval.get("type") != APPROVAL_TYPE or approval.get("status") not in _PENDING:
        return False
    payload = approval.get("payload") or {}
    if payload.get("isol8_action") != marker:
        return False
    return all(payload.get(k) == v for k, v in (extra or {}).items())


async def _file(owner_id: str, *, marker: str, build_payload, issue_id: str | None, dedupe: dict | None) -> str | None:
    """``build_payload(company_name) -> dict`` is called only once we know a
    card actually needs filing — after the company-id gate AND the dedup
    check — so a no-company or already-pending owner never triggers the
    extra ``resolve_company_name`` lookup (matching ``company_stripe_approval``'s
    ordering; a naive "resolve the name up front" ordering was tried and made
    the no-company path issue a live, unmocked Paperclip company lookup)."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        logger.info("provisioning_approvals: no Paperclip company for owner_id=%s", owner_id)
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


async def request_cli_pairing(
    *, owner_id: str, issue_id: str | None, browser_url: str, verification_code: str
) -> str | None:
    return await _file(
        owner_id,
        marker=PAIR_MARKER,
        build_payload=lambda company_name: {
            "title": f"Connect Stripe CLI for {company_name}",
            "browser_url": browser_url,
            "verification_code": verification_code,
        },
        issue_id=issue_id,
        dedupe=None,
    )


async def request_provider_consent(
    *, owner_id: str, issue_id: str | None, provider: str, service: str, terms_url: str, pricing: str
) -> str | None:
    return await _file(
        owner_id,
        marker=CONSENT_MARKER,
        build_payload=lambda company_name: {
            "title": f"Approve {provider} {service} for {company_name}",
            "provider": provider,
            "service": service,
            "terms_url": terms_url,
            "pricing": pricing[:500],
        },
        issue_id=issue_id,
        # Per provider/service: a pending Vercel card must not silently swallow
        # a PostHog request.
        dedupe={"provider": provider, "service": service},
    )
