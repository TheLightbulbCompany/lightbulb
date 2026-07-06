"""Resolve brand-blind ``OwnerFacts`` for ONE owner — shared by every runtime.

The fact-gathering half of OpenClaw's ``friday_identity._resolve_owner_profile``
is runtime-neutral: it reads Clerk (``get_user`` / ``get_organization``), the
founding-owner email lookup, and the billing provider choice. Only the RENDERING
of those facts (a formatted ``provider_label``, IDENTITY/SOUL/USER markdown) is
OpenClaw-specific. This module extracts the neutral gathering into
``resolve_owner_facts`` so BOTH runtimes seed an owner's persona from ONE lookup:
OpenClaw renders its markdown from the facts, Hermes writes a per-run
instructions blob from the same facts.

``resolve_owner_facts`` is a faithful move of ``_resolve_owner_profile``'s
gathering + string-derivation, so OpenClaw's rendered output stays byte-identical
(same facts, same rendering). Its degradation mirrors the original: a missing
Clerk user/org degrades to placeholder strings, never raising on ``None`` — but,
like the original, it does not swallow a raising ``resolve_provider_choice``;
the seed call site guards the whole resolution best-effort.

Import-lightness: only stdlib + the pure ``OwnerFacts`` DTO load at module import.
Clerk / billing / auth imports are lazy inside the function (they pull HTTP + DDB
clients), so importing this module never drags heavy plumbing onto a hot path.
"""

from __future__ import annotations

from core.runtime.base import OwnerFacts


def _is_org_owner_id(owner_id: str) -> bool:
    """Delegates to core.auth.is_org_owner_id — single edit point for the prefix
    convention. Lazy import keeps the module import-light."""
    from core.auth import is_org_owner_id

    return is_org_owner_id(owner_id)


async def resolve_owner_facts(owner_id: str) -> OwnerFacts:
    """Gather the brand-blind facts describing ``owner_id``.

    Branches on ``_is_org_owner_id``:
      - Personal: ``clerk_admin.get_user`` → first/last name + primary email,
        with the same greeting/display fallbacks the OpenClaw seeder used.
      - Org: ``clerk_admin.get_organization`` → org name; ``lookup_owner_email``
        (threaded the org's ``created_by``) → founding-owner email.

    Provider selection: ``container_provision.resolve_provider_choice`` (reads
    billing_repo), defaulting to ``bedrock_claude`` when unset — this returns the
    RAW ``(provider_choice, byo_provider)`` tuple; each runtime formats its own
    label. Missing Clerk fields degrade to placeholder strings ("(unknown)",
    "your team") so a seeder always has usable facts.
    """
    # Lazy imports keep module-load light and avoid import cycles between the
    # seeders and container_provision (which uses clerk_admin).
    from core.services import clerk_admin
    from core.services.container_provision import resolve_provider_choice

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)

    if _is_org_owner_id(owner_id):
        from core.services.paperclip_owner_email import lookup_owner_email

        org = await clerk_admin.get_organization(owner_id)
        org_obj = org or {}
        org_name = org_obj.get("name") or "your team"
        # `lookup_owner_email` short-circuits to None when fallback_user_id is
        # falsy, so threading the org's `created_by` (the founding user_id from
        # Clerk's `GET /organizations/{org_id}`) is what makes this branch
        # actually return a real email instead of always landing on "(unknown)".
        created_by = org_obj.get("created_by") or None
        owner_email = await lookup_owner_email(org_id=owner_id, fallback_user_id=created_by) or "(unknown)"
        return OwnerFacts(
            owner_type="org",
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            display_name=org_name,
            first_name="team",
            full_name=org_name,
            email=owner_email,
            org_name=org_name,
        )

    user = await clerk_admin.get_user(owner_id)
    user = user or {}
    first_name = user.get("first_name") or ""
    last_name = user.get("last_name") or ""
    full_name = f"{first_name} {last_name}".strip() or "(unknown)"

    # Pick the primary email when present; fall back to the first one in the
    # list, then to a placeholder.
    emails = user.get("email_addresses") or []
    primary_id = user.get("primary_email_address_id")
    primary = next((e for e in emails if e.get("id") == primary_id), None)
    if primary is None and emails:
        primary = emails[0]
    email = (primary or {}).get("email_address") or "(unknown)"

    # Friendly fallback for the "what to call them" / greeting slot.
    if first_name:
        first_name_or_fallback = first_name
    elif email != "(unknown)" and "@" in email:
        first_name_or_fallback = email.split("@", 1)[0]
    else:
        first_name_or_fallback = "there"

    owner_display = first_name or first_name_or_fallback

    return OwnerFacts(
        owner_type="personal",
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        display_name=owner_display,
        first_name=first_name_or_fallback,
        full_name=full_name,
        email=email,
        org_name=None,
    )
