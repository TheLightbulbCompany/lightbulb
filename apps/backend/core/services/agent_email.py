"""The workspace's one agent mailbox: ``agents@{slug}.{domain}``.

Owner ruling 2026-10-03: agents have no mailboxes of their own. Every agent
sends from the team address under its own display name (``from_header``), and
inbound to it goes to the CEO unless it answers a thread another agent wrote
(``routers/ses_inbound.py``). One row in ``agent-email-addresses`` holds the
workspace's policy flags; it is created on first use (``ensure_team_address``),
not at provisioning, so the slug derives from the company name the agent chose.
"""

import logging
import re
from email.utils import formataddr

from core.config import settings
from core.repositories import agent_email_repo, company_slug_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_identity import allocate_company_slug
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

EMAIL_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _domain() -> str:
    return (settings.AGENT_EMAIL_DOMAIN or "").strip().lower()


def strictest(rows: list[dict]) -> dict:
    """The strictest of every row's owner-editable settings: closed if any row is closed, paused if
    any is paused, verified senders unioned. No rows is the open default."""
    settings = {
        "send_to_anyone": all(bool(r.get("send_to_anyone")) for r in rows),
        "verified_senders": sorted({s for r in rows for s in r.get("verified_senders") or []}),
    }
    paused = next((r["outbound_disabled"] for r in rows if r.get("outbound_disabled")), None)
    if paused:
        settings["outbound_disabled"] = paused
    return settings


async def team_address(owner_id: str) -> str | None:
    """``agents@{slug}.{domain}``, or None when there is no domain or slug."""
    row = await company_slug_repo.get_by_owner(owner_id)
    slug = row.get("slug") if row else None
    if not _domain() or not slug:
        return None
    return f"agents@{slug.strip().lower()}.{_domain()}"


async def ensure_team_address(owner_id: str, owner_email: str | None) -> tuple[str | None, str]:
    """The team address, its row created now if absent. ``(address, "ok")`` or
    ``(None, reason)`` with reason ``disabled | slug_allocation_failed | mint_failed``.

    A new row lists the owner as a verified sender and is open
    (``send_to_anyone``) unless the owner still has legacy per-agent rows: then
    it is born with the strictest of their settings, so closed email or a
    complaint pause never reopens. An existing row is never rewritten, so the
    owner's settings survive. Never raises.
    """
    try:
        if not _domain():
            return None, "disabled"
        address = await team_address(owner_id)
        if not address:
            onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
            company_id = onboarding.get("company_id")
            name = await resolve_company_name(company_id) if company_id else None
            slug = await allocate_company_slug(owner_id=owner_id, name=name) if name else None
            if not slug:
                return None, "slug_allocation_failed"
            address = f"agents@{slug}.{_domain()}"
        row = await agent_email_repo.get_by_address(address)
        if not row:
            seed = strictest(await agent_email_repo.query_by_owner(owner_id))
            if owner_email:
                seed["verified_senders"] = sorted({*seed["verified_senders"], owner_email})
            row = (
                await agent_email_repo.put(address=address, owner_id=owner_id, **seed)
                # lost a create race: the winner's row
                or await agent_email_repo.get_by_address(address)
            )
        if not row or row.get("owner_id") != owner_id:
            logger.error("agent_email: %s is not owner %s's row", address, owner_id)
            return None, "mint_failed"
        return address, "ok"
    except Exception:  # noqa: BLE001
        logger.exception("agent_email: ensure_team_address failed for %s", owner_id)
        return None, "mint_failed"


async def from_header(owner_id: str, agent_id: str, address: str, agent_name: str | None = None) -> str:
    """``"Friday · Acme agents" <agents@acme.…>`` — the agent's own name on the
    shared address (``agent_name`` when the caller already resolved it). Falls
    back to the bare address when either name is unavailable; never raises,
    never fails a send."""
    try:
        onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
        company_id = onboarding.get("company_id")
        company = await resolve_company_name(company_id) if company_id else None
        agent = agent_name or (await resolve_agent_name(agent_id=agent_id, company_id=company_id) if company else None)
        return formataddr((f"{agent} · {company} agents", address)) if agent else address
    except Exception:  # noqa: BLE001
        logger.warning("agent_email: display name lookup failed for %s/%s", owner_id, agent_id)
        return address


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


async def resolve_agent_name(*, agent_id: str, company_id: str | None) -> str | None:
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
        agent = agent or {}
        agent_company_id = agent.get("companyId")
        if agent_company_id != company_id:
            # get_agent rides the shared admin cookie and is not company-scoped
            # by itself — a container-supplied agent_id from a DIFFERENT
            # owner's company must not mint a foreign agent's name onto this
            # owner's subdomain. A MISSING companyId is unresolved, not
            # authorized — a record with no companyId at all must not be
            # treated as if it belonged to the owner's company.
            if not agent_company_id:
                logger.warning(
                    "agent_email: agent %s has no companyId — refusing name resolution",
                    agent_id,
                )
            else:
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
