"""``agents@{slug}`` accepts a member's meeting invite: one RFC 5546 ``METHOD:REPLY`` to the
organizer, so the event shows the agents as going. Best-effort; ``meeting_invites.handle_team_mail``
keeps it to one per (uid, sequence) via ``meeting.rsvpSequence`` on the room META."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.observability.metrics import put_metric
from core.services import agent_email, agent_mail_send, agent_mail_tenant, ses_email
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS

if TYPE_CHECKING:
    from core.services.meeting_invites import Invite

logger = logging.getLogger(__name__)

_BODY = "Your Lightbulb agents will join. Notes will land in your Lightbulb workspace afterwards."


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _clean(value: str) -> str:
    """Sender-written text minus C0 controls (a bare CR or LF would end the content line)."""
    return re.sub(r"[\x00-\x1f\x7f]", "", value)


def _escape(value: str) -> str:
    """RFC 5545 TEXT: the inverse of ``meeting_invites._text``; other controls are dropped."""
    value = re.sub(r"([\\,;])", r"\\\1", value).replace("\r\n", "\\n").replace("\n", "\\n")
    return _clean(value)


def _fold(line: str) -> str:
    """RFC 5545 §3.1: at most 75 octets per line, continuations start with one space."""
    out, data = [], line.encode()
    while len(data) > (74 if out else 75):
        cut = 74 if out else 75
        while cut and (data[cut] & 0xC0) == 0x80:  # never split a UTF-8 sequence
            cut -= 1
        out.append(data[:cut].decode())
        data = data[cut:]
    return "\r\n ".join([*out, data.decode()])


def reply_ics(invite: Invite, address: str, now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Lightbulb//Meetings//EN",
        "VERSION:2.0",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        # One occurrence of a series: Google matches the series UID plus that RECURRENCE-ID.
        f"UID:{_clean(invite.instance_of or invite.uid)}",
        *([_clean(invite.recurrence_id_line)] if invite.recurrence_id_line else []),
        f"SEQUENCE:{invite.sequence}",
        f"DTSTAMP:{_utc(now)}",
        f"ORGANIZER:mailto:{_clean(invite.organizer_email)}",
        f"ATTENDEE;PARTSTAT=ACCEPTED;CN=Lightbulb agents:mailto:{_clean(address)}",
        f"SUMMARY:{_escape(invite.summary)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(_fold(line) + "\r\n" for line in lines)


async def accept(owner_id: str, invite: Invite) -> bool:
    """True once the REPLY is sent. Never raises: a failed RSVP leaves the room scheduled, and the
    next resend of the invite retries it."""
    try:
        result = await _accept(owner_id, invite)
    except Exception:  # noqa: BLE001 -- the room is the product; the RSVP is courtesy
        logger.warning("meeting %s: RSVP failed", invite.uid, exc_info=True)
        result = "error"
    put_metric("meeting.rsvp", dimensions={"result": result})
    return result == "ok"


async def _accept(owner_id: str, invite: Invite) -> str:
    address = await agent_email.team_address(owner_id)
    if not address or not invite.organizer_email:
        return "no_address"
    # Google applies a REPLY only from an address on the event; a forwarded invite gets the room
    # and the bot, not an RSVP.
    if address.lower() not in {e.lower() for e in invite.attendee_emails}:
        return "not_invited"
    if not agent_email.EMAIL_ADDRESS_RE.match(invite.organizer_email) or re.search(r"[,;<>\s]", invite.organizer_email):
        return "bad_organizer"  # exactly one bare address: a list would be many sends for one cap unit
    # The shared address answers sender-chosen organizers: it draws the owner's daily send cap.
    if not await agent_mail_send.consume_cap(owner_id):
        return "rate_limited"
    sent = await ses_email.send_ses_email(
        from_addr=address,
        to=invite.organizer_email,
        subject=f"Accepted: {' '.join((invite.summary or 'Meeting').split())}",
        body_text=_BODY,
        headers=dict(AGENT_AUTHORED_HEADERS),
        kind="meeting_rsvp",
        tenant_name=await agent_mail_tenant.ensure_tenant(owner_id),
        calendar=reply_ics(invite, address, datetime.now(timezone.utc)),
    )
    return "ok" if sent is not None else "not_sent"
