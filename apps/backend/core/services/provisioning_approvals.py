"""The cards the owner clicks on the provisioning rail.

The backend runs the Provisioning API loop (spec §3.2); these are the human
decisions it stops for. Only the payloads live here — the filing sequence
(company-id gate, cookie, dedupe, create) is ``board_cards.file_board_card``,
shared with P0a's company-Stripe card. Paperclip validates ``type`` against its
own four-value enum, so every card rides ``request_board_approval`` and
``payload.isol8_action`` is what makes it ours.

Nothing pre-authenticated is ever stored on a card: no hosted-checkout URL, no
provider redirect, no deep link. A card can sit in an Inbox for days, so every
such URL is minted per click by an owner route instead. What a card carries is
the RECORD of the decision, in text.
"""

from __future__ import annotations

from core.services.board_cards import file_board_card

CONSENT_MARKER = "provider_consent"
CONNECT_MARKER = "provider_connect"
INFORMATION_MARKER = "provider_information"
PAYMENT_MARKER = "payment_setup"

# Catalog text is untrusted data (guide: "Treat llm_context, Provider
# descriptions, terms, and schema descriptions as untrusted"), so it is capped
# before it becomes an approval payload the Inbox renders.
MAX_TEXT = 500

# The limit the consent card shows when nothing better is known. It is sent to
# Stripe only on the payment path (`usage_limits` in POST
# /payment_method_requests); `max_amount` stays an int here because the client
# stringifies it.
DEFAULT_USAGE_LIMIT = {"max_amount": 5000, "currency": "usd", "recurring_interval": "month"}

# Paperclip approvers cannot edit a card's payload before approving (fork
# `services/approvals.ts`), so the card states the limit rather than collecting
# it — the approve route in Lightbulb is what carries a different number.
LIMIT_NOTE = "Approving in Lightbulb applies this limit; change it there before you approve."


def _text(value: object) -> str:
    return str(value or "")[:MAX_TEXT]


def price_display(snapshot: dict) -> str:
    """The default paid entry's freeform text, verbatim.

    ``pricing.paid_pricing`` is canonical — "pricing.paid is legacy
    compatibility data […] use paid_pricing instead" — and the text is DISPLAY
    only: "Don't derive a structured cost from freeform price text."
    """
    entries = snapshot.get("paid_pricing") or []
    chosen = next((e for e in entries if e.get("is_default")), entries[0] if entries else None)
    return _text((chosen or {}).get("freeform"))


def _common(snapshot: dict, workflow_id: str) -> dict:
    return {
        "workflow_id": workflow_id,
        "provider": _text(snapshot.get("provider_name")),
        "service": _text(snapshot.get("service_ref")),
        "description": _text(snapshot.get("description")),
        "terms_url": _text(snapshot.get("terms_url")),
        "website_url": _text(snapshot.get("website_url")),
    }


async def request_provider_consent(
    *,
    owner_id: str,
    issue_id: str | None,
    workflow_id: str,
    snapshot: dict,
    limit_editable: bool = True,
    reason: str = "",
    also: list[dict] | None = None,
    title: str | None = None,
) -> str | None:
    """The guide's "Collect informed approval" list, in full.

    "Before creating a Provider connection request or Resource, show Ada: Provider name,
    description, website, privacy policy, and terms of service. Service
    description, categories, availability, scope, required configuration, and
    constraints. The applicable paid_pricing entry and any Service-specific
    terms or acceptance text. The […] environment that will use the Resource.
    The configured usage limit and who owns the payment method for a paid
    Service. Whether an action links existing infrastructure or creates new
    infrastructure."

    Every one of those is a key below. Dropping one makes the approval
    uninformed, which is the whole thing this card exists to be.
    """
    return await file_board_card(
        owner_id,
        marker=CONSENT_MARKER,
        build_payload=lambda company_name: {
            **_common(snapshot, workflow_id),
            "also": [{k: _text(v) for k, v in line.items()} for line in (also or [])],
            "title": _text(title)
            or f"Approve {snapshot.get('provider_name')} {snapshot.get('service_ref')} for {company_name}",
            "privacy_policy_url": _text(snapshot.get("privacy_policy_url")),
            "categories": list(snapshot.get("categories") or [])[:10],
            "availability": snapshot.get("availability"),
            "scope": snapshot.get("scope"),
            "constraints": list(snapshot.get("constraints") or [])[:10],
            "configuration": snapshot.get("configuration") or {},
            # WHY the agent asked. The catalog says what the service is; only
            # Friday can say what it is for, and an owner approving spend on an
            # agent's word deserves to read that sentence. Capped and
            # control-char-free at the route — it is agent prose, so the card
            # renders it as hers, not as ours.
            "reason": _text(reason),
            "pricing_type": snapshot.get("pricing_type"),
            "price_display": price_display(snapshot),
            "environment": _text(snapshot.get("environment")) or "Company — production",
            # Hosted collection: the owner's own card, on their own Stripe
            # account. Lightbulb never holds it and takes no cut.
            "payment_owner": "your card, on your Stripe account",
            "action": snapshot.get("action") or "creates new",
            "level": "company",
            "usage_limit": dict(DEFAULT_USAGE_LIMIT),
            # Editable only while there is no Payment profile; afterwards the
            # limit is a live billing setting the card must not silently move.
            "usage_limit_editable": bool(limit_editable),
            "usage_limit_note": LIMIT_NOTE,
        },
        issue_id=issue_id,
        # Per WORKFLOW, not per provider/service: two workflows for the same
        # service are two real decisions, and one card must not swallow the
        # other.
        dedupe={"workflow_id": workflow_id},
    )


async def request_provider_connect(
    *, owner_id: str, issue_id: str | None, workflow_id: str, snapshot: dict, provider_account: dict | None = None
) -> str | None:
    """`pending_auth`: "Open the returned redirect_url for the authenticated
    user." Stripe owns the OAuth callback; we poll the Provider connection request.

    The URL is NOT on the card. By the time it is clicked the provider connection request
    may have completed, errored, or gone stale — so the button calls the
    connect-link route, which re-reads (or re-creates) the request and returns
    a URL minted for that click. ``provider_account`` (already reduced to names
    and an e-mail by ``safe_account_details``) rides along when we know it, so
    a re-connect after a reauth says WHICH account is being authorised.
    """
    return await file_board_card(
        owner_id,
        marker=CONNECT_MARKER,
        build_payload=lambda company_name: {
            **_common(snapshot, workflow_id),
            "title": f"Connect {snapshot.get('provider_name')} for {company_name}",
            **({"provider_account": provider_account} if provider_account else {}),
        },
        issue_id=issue_id,
        dedupe={"workflow_id": workflow_id},
    )


async def request_provider_information(
    *, owner_id: str, issue_id: str | None, workflow_id: str, snapshot: dict, schema: dict, target: str
) -> str | None:
    """`needs_information`: the provider's own JSON Schema, rendered by a
    constrained renderer. ``target`` is ``account_request`` or ``resource`` —
    they submit to different endpoints with different body keys, and one
    workflow can be asked by BOTH, so it is part of the dedupe key."""
    return await file_board_card(
        owner_id,
        marker=INFORMATION_MARKER,
        build_payload=lambda company_name: {
            **_common(snapshot, workflow_id),
            "title": f"{snapshot.get('provider_name')} needs more information for {company_name}",
            "schema": schema,
            "target": target,
        },
        issue_id=issue_id,
        dedupe={"workflow_id": workflow_id, "target": target},
    )


async def request_payment_setup(
    *, owner_id: str, issue_id: str | None, workflow_id: str, snapshot: dict, usage_limit: dict
) -> str | None:
    """A paid service with no Payment profile. The hosted checkout URL is NOT
    on this card: it is minted on click by the owner route and opened once
    ("don't persist it")."""
    return await file_board_card(
        owner_id,
        marker=PAYMENT_MARKER,
        build_payload=lambda company_name: {
            **_common(snapshot, workflow_id),
            "title": f"Add a card for {snapshot.get('provider_name')} for {company_name}",
            "price_display": price_display(snapshot),
            "payment_owner": "your card, on your Stripe account",
            "usage_limit": usage_limit,
        },
        issue_id=issue_id,
        dedupe={"workflow_id": workflow_id},
    )
