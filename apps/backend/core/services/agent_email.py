"""Provisioned agent email addresses.

The address is built from the owner's company slug, which the Company
Identity work already reserved, as a per-company SUBDOMAIN: each agent gets
``{agent}@{slug}.{domain}`` — e.g. ``friday@acme-robotics.isol8.co``. The
agent name is the mailbox; the company slug is its own subdomain, not part
of the local part. Because every agent is a distinct mailbox on the
company's own subdomain, two agents of the same owner can never collide,
and no truncation/disambiguation machinery is needed — the slug is already
bounded (``company_identity.slugify`` caps it at 32 chars, a legal DNS
label) and the agent name only has to be a legal local part, not fit
alongside the slug in a single RFC 5321 budget.

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


def _agent_slug(agent_name: str) -> str:
    slug = _SLUG_RE.sub("-", agent_name.strip().lower()).strip("-")
    return slug or "agent"


def build_agent_email_address(*, slug: str, agent_name: str, domain: str) -> str:
    """``{agent}@{slug}.{domain}`` — the agent name is the mailbox, the company
    slug is its own subdomain. e.g. ``friday@acme-robotics.isol8.co``.

    Deterministic and lowercased. ``slug`` is already a valid DNS label
    (company_identity.slugify → ≤32 chars, ``[a-z0-9-]``); the agent name is
    slugified into the local part. No truncation/disambiguation is needed —
    each agent is a distinct mailbox on the company's own subdomain, so two
    agents of the same owner can never collide (this deletes the prior
    per-agent address-collision bug)."""
    company = slug.strip().lower()
    agent = _agent_slug(agent_name)
    return f"{agent}@{company}.{domain.strip().lower()}"


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

        address = build_agent_email_address(slug=slug, agent_name=agent_name, domain=domain)

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


def sender_allowed(row: dict, sender: str) -> bool:
    """True when ``sender`` may WAKE this address's agent: an exact entry in
    ``verified_senders`` or an ``@domain`` entry matching the sender's domain
    exactly (no subdomain matching — ``@clientco.com`` does not admit
    ``x@sub.clientco.com``)."""
    sender = (sender or "").strip().lower()
    if not sender or "@" not in sender:
        return False
    domain = "@" + sender.rsplit("@", 1)[1]
    for entry in row.get("verified_senders") or []:
        entry = (entry or "").strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if entry == domain:
                return True
        elif entry == sender:
            return True
    return False
