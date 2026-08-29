"""The two cards the owner must click for the provisioning rail.

Friday runs the Stripe CLI herself; the platform's job is to put the two human
decisions in front of the owner, in the surface they already use:

  * ``stripe_cli_login`` — she ran ``stripe login --non-interactive``, which
    printed a browser URL and a verification code. The owner opens the URL,
    confirms the code, and approves the card. Friday polls and continues.
  * ``provider_consent`` — before a paid or ToS-bearing service, the owner sees
    the provider, the service, the terms link and the price, and approves.
    Only then may Friday pass ``--accept-tos``.

Only the payloads live here — the filing sequence (company-id gate, cookie,
dedupe, create) is ``board_cards.file_board_card``, shared with P0a's
company-Stripe card.
"""

from __future__ import annotations

from core.services.board_cards import file_board_card

PAIR_MARKER = "stripe_cli_login"
CONSENT_MARKER = "provider_consent"


async def request_cli_pairing(
    *, owner_id: str, issue_id: str | None, browser_url: str, verification_code: str
) -> str | None:
    return await file_board_card(
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
