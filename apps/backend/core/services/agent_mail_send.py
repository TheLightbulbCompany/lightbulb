"""The ONE outbound door for agent mail.

Three recipient kinds, one policy, evaluated server-side so nothing the agent
passes can widen it:

| recipient                       | allowed when                                   |
|----------------------------------|------------------------------------------------|
| owner (no ``to``/``reply_key``) | always — delegates to ``send_to_owner``        |
| ``reply_key`` (stored inbound)  | row not ``outbound_disabled``; key is owner's; sender is owner/allowlisted, OR ``send_to_anyone`` |
| arbitrary ``to``                | row ``send_to_anyone`` and not ``outbound_disabled`` |

Every agent sends from the workspace's ONE address (``agents@{slug}``) under its
own name, so every flag below is read from that one row and binds every agent.

``send_to_anyone`` is the ONE "open email" switch: new rows are minted with it ON
(owner ruling 2026-09-24), and ``routers/ses_inbound.py`` reads the same flag to
let any authenticated sender WAKE the agent. Turning it off (Control → Agents →
Email) restores the closed policy in both directions: only the owner and the
``verified_senders`` allowlist may wake the agent, the agent can email only the
owner and reply (``reply_key``) to those senders, and an arbitrary ``to`` is
refused. Rows minted before the ruling were switched on by a one-off repair script.

All paths share the per-owner durable daily cap and are stamped with the
owner's SES tenant. Never raises — the router returns the result string.
"""

import logging
from datetime import datetime, timezone
from email.utils import getaddresses

from core.auth import is_org_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, agent_mail_inbox_repo, container_repo
from core.repositories.agent_mail_inbox_repo import get_message, parse_reply_key
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import ses_email
from core.services.agent_email import (
    EMAIL_ADDRESS_RE,
    append_agent_mail_footer,
    ensure_team_address,
    from_header,
    resolve_agent_name,
    sender_allowed,
    team_address,
)
from core.services.agent_mail_owner_send import DAILY_SEND_CAP, _env_subject, send_to_owner
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS, send_reply
from core.services.agent_mail_tenant import ensure_tenant
from core.services.paperclip_owner_email import lookup_owner_email

logger = logging.getLogger(__name__)


def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v or ""
    return ""


def _single_address(to: str | None) -> str | None:
    """The one bare address ``to`` names, or None if it names zero, more than
    one, or a malformed address. ``send_ses_email`` puts ``to`` straight into
    the MIME ``To:`` header with no ``Destination`` override, so a raw
    comma-joined string IS the SES recipient list — this is the only gate
    between one daily-cap unit and an arbitrary-size blast from a verified
    isol8.co subdomain."""
    addrs = [a for _, a in getaddresses([to or ""]) if a]
    if len(addrs) != 1 or not EMAIL_ADDRESS_RE.match(addrs[0]):
        return None
    return addrs[0]


async def consume_cap(owner_id: str) -> bool:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP)


async def _owner_email(owner_id: str) -> str | None:
    onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
    return await lookup_owner_email(
        org_id=owner_id if is_org_owner_id(owner_id) else None,
        fallback_user_id=onboarding.get("user_id") or owner_id,
    )


async def send(
    *,
    owner_id: str,
    agent_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    to: str | None = None,
    reply_key: str | None = None,
    purpose: str = "routine",
) -> str:
    path = "reply" if reply_key else "free" if to else "owner"
    try:
        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            return _done(path, "disabled")
        # ponytail: the "no shell beyond adapters" allowlist in the spec is not enforceable while OpenCLI needs the terminal; the doors we own (mail) are fenced, the rest is SOUL. Revisit with a Hermes per-tool allowlist.
        line = await agent_email_repo.text_line_for_owner(owner_id)
        if line and line.get("agent_id") == agent_id:
            return _done(path, "front_desk_agent")
        if to and reply_key:
            return _done(path, "error")
        tenant = await ensure_tenant(owner_id)

        if path == "owner":
            result = await send_to_owner(
                owner_id=owner_id,
                purpose=purpose,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                agent_id=agent_id,
                tenant_name=tenant,
            )
            return _done(path, result)

        if path == "reply":
            parsed = parse_reply_key(reply_key or "")
            if not parsed:
                return _done(path, "bad_reply_key")
            address, received_key = parsed
            msg = await get_message(address, received_key)
            if (
                not msg
                or msg.get("owner_id") != owner_id
                or msg.get("direction") == "out"
                or msg.get("channel") == "text"
            ):
                return _done(path, "bad_reply_key")
            # Stored state decides the replying agent — never fall back to the
            # caller-supplied agent_id, which would let a container claim to
            # be replying as an agent it isn't.
            msg_agent_id = msg.get("agent_id")
            if not msg_agent_id:
                return _done(path, "bad_reply_key")
            # The flags live on the workspace's one row, which send_reply sends from.
            from_addr = await team_address(owner_id)
            row = await agent_email_repo.get_by_address(from_addr) if from_addr else None
            if not row:
                return _done(path, "no_address")
            if row.get("outbound_disabled"):
                return _done(path, "outbound_disabled")
            # A stranger who emailed first does not thereby earn a reply — only
            # the owner/allowlist, or a row with ``send_to_anyone`` on (the
            # default), may receive agent-authored mail.
            sender = msg.get("from_addr") or ""
            if not sender_allowed(row, sender) and not row.get("send_to_anyone"):
                return _done(path, "forbidden_recipient")
            if not await consume_cap(owner_id):
                return _done(path, "rate_limited")
            headers = msg.get("headers") or {}
            result = await send_reply(
                ctx={
                    "owner_id": owner_id,
                    "agent_id": msg_agent_id,
                    "reply_to": msg.get("from_addr") or "",
                    "in_reply_to": _header(headers, "Message-ID"),
                    "references": _header(headers, "References"),
                    "subject": msg.get("subject") or "",
                },
                body_text=body_text,
                body_html=body_html,
                tenant_name=tenant,
            )
            return _done(path, "error" if result == "not_sent" else result)

        # free
        to_addr = _single_address(to)
        if not to_addr:
            return _done(path, "forbidden_recipient")
        # The caller names the sending agent; it must be one of this workspace's
        # (the registry is keyed by workspace), or a reply would route to a stranger.
        agent_name = await resolve_agent_name(owner_id=owner_id, agent_id=agent_id)
        if not agent_name:
            return _done(path, "unknown_agent")
        owner_email = await _owner_email(owner_id)
        address, _reason = await ensure_team_address(owner_id, owner_email)
        if not address:
            return _done(path, "no_address")
        row = await agent_email_repo.get_by_address(address) or {}
        if row.get("outbound_disabled"):
            return _done(path, "outbound_disabled")
        if not row.get("send_to_anyone"):
            return _done(path, "forbidden_recipient")
        if not await consume_cap(owner_id):
            return _done(path, "rate_limited")
        body_text, body_html = await append_agent_mail_footer(
            owner_id=owner_id,
            agent_id=agent_id,
            address=address,
            body_text=body_text,
            body_html=body_html,
            agent_name=agent_name,
        )
        sent_subject = _env_subject(subject)
        message_id = await ses_email.send_ses_email(
            from_addr=await from_header(owner_id, agent_id, address, agent_name=agent_name),
            to=to_addr,
            subject=sent_subject,
            body_text=body_text,
            body_html=body_html,
            kind="agent_free_send",
            headers=dict(AGENT_AUTHORED_HEADERS),
            tenant_name=tenant,
        )
        if not message_id:
            return _done(path, "error")
        await agent_mail_inbox_repo.put_sent(
            address=address,
            message_id=message_id,
            owner_id=owner_id,
            agent_id=agent_id,
            to=to_addr,
            subject=sent_subject,
            text=body_text,
        )
        return _done(path, "sent")
    except Exception:
        logger.exception("agent_mail_send failed owner=%s agent=%s path=%s", owner_id, agent_id, path)
        return _done(path, "error")


def _done(path: str, result: str) -> str:
    put_metric("agent_email.send", dimensions={"path": path, "result": result})
    return result
