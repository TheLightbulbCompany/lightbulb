"""The cards the owner clicks on the provisioning rail.

The backend runs the Provisioning API loop (spec §3.2); these are the human
decisions it stops for. Only the payloads live here — the filing sequence
(company-id gate, cookie, dedupe, create) is ``board_cards.file_board_card``,
shared with P0a's company-Stripe card. Paperclip validates ``type`` against its
own four-value enum, so every card rides ``request_board_approval`` and
``payload.isol8_action`` is what makes it ours.
"""

from __future__ import annotations

from core.services.board_cards import file_board_card

CONSENT_MARKER = "provider_consent"


async def request_provider_consent(
    *, owner_id: str, issue_id: str | None, provider: str, service: str, terms_url: str, pricing: str
) -> str | None:
    return await file_board_card(
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
