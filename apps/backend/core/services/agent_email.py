"""Provisioned agent email addresses.

The address is built from the owner's company slug, which the Company
Identity work already reserved: the primary agent (Friday) gets
``{slug}@{domain}`` — a company's main address is its CEO's — and any
additional agent gets ``{slug}-{agent}@{domain}``.

The slug is always passed IN, never looked up here. A rare provisioning
re-entry can leave an owner holding two slug rows, and
``company_slug_repo.get_by_owner`` uses ``Limit=1`` with no ordering, so
reading it back would be nondeterministic. Callers pass the slug they just
allocated.

Minting is best-effort by contract — ``provision_org`` calls it after
seeding Friday, and a mail-side failure must never undo a good provision.
"""

import logging
import re

from core.config import settings
from core.repositories import agent_email_repo

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# RFC 5321 §4.5.3.1.1 caps the local part at 64 octets. The company slug is
# bounded (company_identity.MAX_SLUG_LEN = 32) but an agent name is not, so
# the secondary form has to be truncated or we would persist an address no
# mail system will accept.
MAX_LOCAL_PART_LEN = 64


def _agent_slug(agent_name: str) -> str:
    slug = _SLUG_RE.sub("-", agent_name.strip().lower()).strip("-")
    return slug or "agent"


def build_agent_email_address(*, slug: str, agent_name: str, domain: str, is_primary: bool) -> str:
    """``{slug}@{domain}`` for the primary agent, ``{slug}-{agent}@{domain}``
    otherwise. Deterministic, lowercased, and never longer than
    ``MAX_LOCAL_PART_LEN`` in the local part."""
    local = slug.strip().lower()
    if not is_primary:
        # Truncate the AGENT segment only — the company slug is the owner's
        # identity and must survive intact, and trimming the whole joined
        # string could clip the separator and collide with the primary.
        budget = MAX_LOCAL_PART_LEN - len(local) - 1
        agent = _agent_slug(agent_name)[:budget].strip("-") if budget > 0 else ""
        local = f"{local}-{agent or 'agent'[: max(budget, 1)]}"
    return f"{local[:MAX_LOCAL_PART_LEN]}@{domain.strip().lower()}"


async def mint_agent_email_address(
    *,
    owner_id: str,
    agent_id: str,
    agent_name: str,
    owner_email: str,
    slug: str,
    is_primary: bool = True,
) -> str | None:
    """Mint (or re-affirm) an agent's address row.

    Ownership is checked, not assumed: if the address already exists for a
    DIFFERENT owner, that's a collision — logged loudly (``logger.error``,
    naming the address and both owner ids) and treated as a failed mint, not
    a silent success. ``agent_email_repo.put`` is itself a conditional put
    (mirrors ``company_slug_repo.reserve``), so a race losing there also
    fails the mint rather than overwriting the winner's row.

    Returns the address, or None when the feature is unconfigured, the owner
    has no slug, the address is already held by another owner, or the
    conditional put lost a race. Never raises.
    """
    try:
        domain = (settings.AGENT_EMAIL_DOMAIN or "").strip()
        if not domain:
            logger.debug("agent_email.mint: AGENT_EMAIL_DOMAIN unset; skipping %s", owner_id)
            return None

        if not slug or not slug.strip():
            logger.debug("agent_email.mint: no slug for owner %s; skipping", owner_id)
            return None

        address = build_agent_email_address(slug=slug, agent_name=agent_name, domain=domain, is_primary=is_primary)

        existing = await agent_email_repo.get_by_address(address)
        if existing is not None:
            if existing.get("owner_id") == owner_id:
                return address
            logger.error(
                "agent_email.mint: address collision for %s — held by owner %s, requested by owner %s",
                address,
                existing.get("owner_id"),
                owner_id,
            )
            return None

        row = await agent_email_repo.put(
            address=address,
            owner_id=owner_id,
            agent_id=agent_id,
            verified_senders=[owner_email] if owner_email else [],
        )
        if row is None:
            logger.error(
                "agent_email.mint: conditional put lost a race for %s (owner %s)",
                address,
                owner_id,
            )
            return None
        logger.info("agent_email.mint: minted %s for owner %s", address, owner_id)
        return address
    except Exception as e:  # noqa: BLE001 — minting must never break provisioning
        logger.warning("agent_email.mint: failed for owner %s: %s", owner_id, e)
        return None
