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


def _agent_slug(agent_name: str) -> str:
    slug = _SLUG_RE.sub("-", agent_name.strip().lower()).strip("-")
    return slug or "agent"


def build_agent_email_address(*, slug: str, agent_name: str, domain: str, is_primary: bool) -> str:
    """``{slug}@{domain}`` for the primary agent, ``{slug}-{agent}@{domain}``
    otherwise. Deterministic and lowercased."""
    local = slug.strip().lower()
    if not is_primary:
        local = f"{local}-{_agent_slug(agent_name)}"
    return f"{local}@{domain.strip().lower()}"


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

    Returns the address, or None when the feature is unconfigured, the owner
    has no slug, or minting failed. Never raises.
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
        if await agent_email_repo.get_by_address(address) is not None:
            return address
        await agent_email_repo.put(
            address=address,
            owner_id=owner_id,
            agent_id=agent_id,
            verified_senders=[owner_email] if owner_email else [],
        )
        logger.info("agent_email.mint: minted %s for owner %s", address, owner_id)
        return address
    except Exception as e:  # noqa: BLE001 — minting must never break provisioning
        logger.warning("agent_email.mint: failed for owner %s: %s", owner_id, e)
        return None
