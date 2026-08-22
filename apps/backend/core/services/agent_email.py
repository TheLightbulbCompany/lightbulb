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
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_identity import allocate_company_slug
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie

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


async def resolve_company_name(company_id: str) -> str | None:
    """The company's CURRENT name from Paperclip, or None. Read live, never
    cached — the agent renames the company during its first run and that
    rename lands in Paperclip, not in our row."""
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        company = await admin.get_company(session_cookie=cookie, company_id=company_id)
        name = (company or {}).get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:  # noqa: BLE001 — caller maps None to a failed mint
        logger.warning("agent_email: company-name lookup failed for %s: %s", company_id, e)
        return None


async def resolve_agent_name(*, agent_id: str, friday_agent_id: str | None, company_id: str | None) -> str | None:
    if friday_agent_id and agent_id == friday_agent_id:
        return "Friday"
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
        agent = agent or {}
        agent_company_id = agent.get("companyId")
        if agent_company_id and agent_company_id != company_id:
            # get_agent rides the shared admin cookie and is not company-scoped
            # by itself — a container-supplied agent_id from a DIFFERENT
            # owner's company must not mint a foreign agent's name onto this
            # owner's subdomain.
            logger.warning(
                "agent_email: agent %s belongs to company %s, not owner's company %s — refusing name resolution",
                agent_id,
                agent_company_id,
                company_id,
            )
            return None
        name = agent.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("agent_email: agent-name lookup failed for %s: %s", agent_id, e)
        return None


async def ensure_agent_address(*, owner_id: str, agent_id: str, owner_email: str) -> tuple[str | None, str]:
    """The active address for ``agent_id`` — minted now if it does not exist.

    Returns ``(address, "ok")`` or ``(None, reason)``. Minting is deferred to
    first use on purpose: the slug derives from the company name the agent
    actually chose (see agent_mail_owner_send's module docstring). Never raises.
    """
    try:
        for row in await agent_email_repo.query_by_owner(owner_id):
            if row.get("agent_id") == agent_id and row.get("status") == "active" and row.get("address"):
                return row["address"], "ok"

        onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
        company_id = onboarding.get("company_id")
        if not company_id:
            return None, "incomplete_provisioning"
        company_name = await resolve_company_name(company_id)
        if not company_name:
            return None, "company_name_unavailable"
        slug = await allocate_company_slug(owner_id=owner_id, name=company_name)
        if not slug:
            return None, "slug_allocation_failed"
        agent_name = await resolve_agent_name(
            agent_id=agent_id, friday_agent_id=onboarding.get("friday_agent_id"), company_id=company_id
        )
        if not agent_name:
            return None, "agent_name_unavailable"
        address = await mint_agent_email_address(
            owner_id=owner_id, agent_id=agent_id, agent_name=agent_name, owner_email=owner_email, slug=slug
        )
        return (address, "ok") if address else (None, "mint_failed")
    except Exception:  # noqa: BLE001
        logger.exception("agent_email: ensure_agent_address failed for %s/%s", owner_id, agent_id)
        return None, "mint_failed"
