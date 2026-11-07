"""The workspace's one agent mailbox: ``agents@{slug}.{domain}``.

Owner ruling 2026-10-03: agents have no mailboxes of their own. Every agent
sends from the team address under its own display name (``from_header``), and
inbound to it goes to the CEO unless it answers a thread another agent wrote
(``routers/ses_inbound.py``). One row in ``agent-email-addresses`` holds the
workspace's policy flags; it is created on first use (``ensure_team_address``),
not at provisioning, so the slug derives from the workspace name (``workspace_name``).

That slug-form address is the ROUTING address: every key, inbound lookup and
inbox row uses it. A row may also carry a ``public_address`` (set only by
``scripts/set_team_mail_identity.py``), which is what the workspace SENDS as
and SHOWS; mail to it is forwarded to the routing address outside SES.
"""

import asyncio
import html
import logging
import re
from email.utils import formataddr

from core.config import settings
from core.repositories import agent_email_repo, company_slug_repo
from core.services import agent_registry
from core.services.company_identity import allocate_company_slug
from core.services.workspace_name import workspace_name

logger = logging.getLogger(__name__)

EMAIL_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def append_agent_mail_footer(
    *,
    owner_id: str,
    agent_id: str,
    address: str,
    body_text: str,
    body_html: str | None,
    agent_name: str | None = None,
) -> tuple[str, str | None]:
    """Append the default-on attribution footer without risking delivery."""
    try:
        row = await agent_email_repo.get_by_address(address) or {}
        if row.get("footer_enabled") is False:
            return body_text, body_html
        company = await workspace_name(owner_id)
        agent = agent_name or await resolve_agent_name(owner_id=owner_id, agent_id=agent_id)
        if not agent or not company:
            logger.warning("agent_email: footer identity lookup failed for %s/%s", owner_id, agent_id)
            return body_text, body_html
        url = f"{settings.FRONTEND_URL.rstrip('/')}?utm_source=agent_email"
        rendered_text = f"{body_text.rstrip()}\n\nSent by {agent} for {company} with Lightbulb ({url})."
        rendered_html = body_html
        if body_html is not None:
            rendered_html = (
                f"{body_html.rstrip()}<p>Sent by {html.escape(agent)} for {html.escape(company)} with "
                f'<a href="{html.escape(url, quote=True)}">Lightbulb</a>.</p>'
            )
        return rendered_text, rendered_html
    except Exception:  # noqa: BLE001 -- attribution lookup must never block mail
        logger.warning("agent_email: footer lookup failed for %s/%s", owner_id, agent_id, exc_info=True)
        return body_text, body_html


def _domain() -> str:
    return (settings.AGENT_EMAIL_DOMAIN or "").strip().lower()


def strictest(rows: list[dict]) -> dict:
    """The strictest of every row's owner-editable settings: closed if any row is closed, paused if
    any is paused, verified senders unioned. No rows is the open default."""
    rows = [
        r for r in rows if not r.get("alias_of") and not r.get("kind")
    ]  # alias and text-line rows carry no settings
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


async def public_for(address: str) -> str:
    """What the routing ``address`` sends and shows as: its row's ``public_address``, else
    itself. Never raises: a failed read sends from the routing address, which still delivers."""
    try:
        row = await agent_email_repo.get_by_address(address) or {}
    except Exception:  # noqa: BLE001
        logger.warning("agent_email: public address lookup failed for %s", address, exc_info=True)
        return address
    return row.get("public_address") or address


_MAIL_CHANGED_TIMEOUT_S = 3.0
_nudges: set[asyncio.Task] = set()


async def mail_changed(owner_id: str) -> None:
    """Tell the workspace's open Mail panels to re-read: a mail was stored or a meeting changed.
    Fire-and-forget: the publish runs off the caller's path (SES inbound, the meeting lease), is
    bounded, and never raises. The panel debounces repeats and also re-reads on focus."""
    task = asyncio.create_task(_publish_mail_changed(owner_id))
    _nudges.add(task)
    task.add_done_callback(_nudges.discard)


async def _publish_mail_changed(owner_id: str) -> None:
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    try:
        await asyncio.wait_for(
            publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": "mail"})),
            _MAIL_CHANGED_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001
        logger.warning("agent_email: mail live event failed owner=%s", owner_id, exc_info=True)


async def public_address(owner_id: str) -> str | None:
    """The workspace's public address (``public_for`` its team address), or None."""
    address = await team_address(owner_id)
    return await public_for(address) if address else None


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
            slug = await allocate_company_slug(owner_id=owner_id, name=await workspace_name(owner_id))
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
    shared address (``agent_name`` when the caller already resolved it). The
    routing ``address`` is swapped for its public address (``public_for``), so
    every agent send goes out as it. Falls back to the bare address when either
    name is unavailable; never raises, never fails a send."""
    address = await public_for(address)
    try:
        company = await workspace_name(owner_id)
        agent = agent_name or await resolve_agent_name(owner_id=owner_id, agent_id=agent_id)
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


async def resolve_agent_name(*, owner_id: str, agent_id: str) -> str | None:
    """The agent's name from this workspace's registry, or None (not this workspace's agent,
    deleted, or a failed read). Scoped by ``owner_id``, so a foreign agent id never resolves."""
    try:
        agent = await agent_registry.get(owner_id, agent_id) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("agent_email: agent-name lookup failed for %s: %s", agent_id, e)
        return None
    name = agent.get("name") if agent.get("status") != "deleted" else None
    return name.strip() if isinstance(name, str) and name.strip() else None
