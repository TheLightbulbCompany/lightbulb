"""``agents@{slug}`` accepts a member's meeting invite: one RFC 5546 ``METHOD:REPLY`` to the
organizer, so the event shows the agents as going. Best-effort and idempotent per (uid, sequence)
via ``meeting.rsvpSequence`` on the room META."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import agent_email, agent_mail_tenant, meeting_invites, ses_email
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS

logger = logging.getLogger(__name__)

_BODY = "Your Lightbulb agents will join. Notes will land in your Lightbulb workspace afterwards."


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(value: str) -> str:
    """RFC 5545 TEXT: the inverse of ``meeting_invites._text``."""
    return re.sub(r"([\\,;])", r"\\\1", value).replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 §3.1: at most 75 octets per line, continuations start with one space."""
    out, data = [], line.encode()
    while len(data) > 75:
        cut = 75 if not out else 74
        while cut and (data[cut] & 0xC0) == 0x80:  # never split a UTF-8 sequence
            cut -= 1
        out.append(data[:cut].decode())
        data = data[cut:]
    return "\r\n ".join([*out, data.decode()])


def reply_ics(invite: meeting_invites.Invite, address: str, now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Lightbulb//Meetings//EN",
        "VERSION:2.0",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        f"UID:{invite.uid}",
        f"SEQUENCE:{invite.sequence}",
        f"DTSTAMP:{_utc(now)}",
        f"ORGANIZER:mailto:{invite.organizer_email}",
        f"ATTENDEE;PARTSTAT=ACCEPTED;CN=Lightbulb agents:mailto:{address}",
        f"DTSTART:{_utc(invite.start)}",
        *([f"DTEND:{_utc(invite.end)}"] if invite.end else []),
        f"SUMMARY:{_escape(invite.summary)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(_fold(line) + "\r\n" for line in lines)


async def accept(owner_id: str, invite: meeting_invites.Invite, room_id: str) -> None:
    """Never raises: an RSVP that fails leaves the room scheduled, and the next resend retries it."""
    try:
        result = await _accept(owner_id, invite, room_id)
    except Exception:  # noqa: BLE001 -- the room is the product; the RSVP is courtesy
        logger.warning("meeting room %s: RSVP failed", room_id, exc_info=True)
        result = "error"
    put_metric("meeting.rsvp", dimensions={"result": result})


async def _accept(owner_id: str, invite: meeting_invites.Invite, room_id: str) -> str:
    meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
    if meeting.get("rsvpSequence") == invite.sequence:
        return "duplicate"
    address = await agent_email.team_address(owner_id)
    if not address or not invite.organizer_email:
        return "no_address"
    sent = await ses_email.send_ses_email(
        from_addr=address,
        to=invite.organizer_email,
        subject=f"Accepted: {invite.summary or 'Meeting'}",
        body_text=_BODY,
        headers=dict(AGENT_AUTHORED_HEADERS),
        kind="meeting_rsvp",
        tenant_name=await agent_mail_tenant.ensure_tenant(owner_id),
        calendar=reply_ics(invite, address, datetime.now(timezone.utc)),
    )
    if sent is None:
        return "not_sent"
    await meeting_invites._patch_meeting(room_id, {**meeting, "rsvpSequence": invite.sequence})
    return "ok"
