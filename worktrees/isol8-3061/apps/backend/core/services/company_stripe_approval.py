"""Friday asks the owner to set up the company's Stripe account.

Files a Paperclip approval so it lands in the Teams inbox (and on the issue
that needed it). The card is informational — the owner acts on it from the
row's "Set up Stripe" button, which calls ``POST /company/stripe/create`` and
then approves the request. Never a gate: if the owner ignores it, chat keeps
working.

Only the billing pre-check and the payload live here; the filing sequence is
``board_cards.file_board_card``.
"""

from __future__ import annotations

from core.repositories import billing_repo
from core.services.board_cards import file_board_card

ACTION_MARKER = "company_stripe"


async def request_company_stripe(*, owner_id: str, issue_id: str | None, reason: str) -> str | None:
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        return None
    return await file_board_card(
        owner_id,
        marker=ACTION_MARKER,
        build_payload=lambda company_name: {
            "title": f"Set up {company_name} on Stripe",
            "reason": (reason or "")[:500],
        },
        issue_id=issue_id,
        # One pending "set up Stripe" card at a time — there is only ever one
        # company Stripe account, so the marker alone is the whole identity.
        dedupe=None,
    )
