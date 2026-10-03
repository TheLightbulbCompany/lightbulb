"""Calendar invites to the team address ``agents@{slug}``: parse the ``text/calendar`` part and
turn a workspace member's invite into a meeting row. Everything else -- plain team mail and a
stranger's invite alike -- is ordinary mail for the CEO, delivered by the inbound route exactly
as mail to the CEO's own address would be (``team_row``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.repositories import agent_email_repo, meetings_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin
from core.services.agent_email import sender_allowed
from core.services.agent_mail_inbound import authenticated_sender, parse_from_address

logger = logging.getLogger(__name__)

_MEET_URL = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")
_LINE = re.compile(r'^((?:[^:"]|"[^"]*")*):(.*)$')  # name;params:value -- a quoted param may hold ':'


@dataclass(frozen=True)
class Invite:
    uid: str
    sequence: int
    method: str  # "REQUEST" | "CANCEL"
    start: datetime
    end: datetime | None
    summary: str
    description: str
    organizer_email: str
    attendee_emails: list[str]
    meeting_url: str | None


def _text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _email(value: str) -> str:
    return re.sub(r"(?i)^mailto:", "", value).strip().lower()


def _when(params: str, value: str) -> datetime:
    value = value.strip()
    if "VALUE=DATE" in params.upper() and "VALUE=DATE-TIME" not in params.upper():
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    tzid = re.search(r'TZID="?([^";:]+)', params)
    # ponytail: Windows TZIDs (Outlook) unsupported → invite stays mail; read the VTIMEZONE offsets when an Outlook+Meet customer appears.
    zone = ZoneInfo(tzid.group(1)) if tzid else timezone.utc
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone)


def parse(calendar_text: str) -> Invite | None:
    """The first VEVENT of an iCalendar body, or None for anything it cannot use: no VEVENT, no
    UID, an oversized UID, a single-instance RECURRENCE-ID edit, or any unreadable value. Total:
    it never raises, so a malformed invite is mail, never a 5xx."""
    try:
        return _parse(calendar_text)
    except Exception:  # noqa: BLE001 -- sender-written input; no content in the log
        logger.debug("meeting invite: unparseable calendar part")
        return None


def _parse(calendar_text: str) -> Invite | None:
    text = re.sub(r"\n[ \t]", "", (calendar_text or "").replace("\r\n", "\n"))
    method, stack, props, attendees = "REQUEST", [], {}, []
    for line in text.split("\n"):
        m = _LINE.match(line)
        if not m:
            continue
        head, value = m.groups()
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "BEGIN":
            stack.append(value.strip().upper())
        elif name == "END":
            if (stack.pop() if stack else "") == "VEVENT":
                break
        elif name == "METHOD" and not stack[1:]:
            method = value.strip().upper()
        elif stack[-1:] == ["VEVENT"]:  # skips VALARM/VTIMEZONE, which carry their own DESCRIPTION/DTSTART
            if name == "ATTENDEE":
                attendees.append(_email(value))
            props.setdefault(name, (params, value))
    uid = props.get("UID", ("", ""))[1].strip()
    # ponytail: RRULE series get only the row the first invite wrote; per-instance handling when a recurring customer meeting appears.
    if not uid or len(uid) > 512 or "DTSTART" not in props or "RECURRENCE-ID" in props:
        return None
    start = _when(*props["DTSTART"])
    end = _when(*props["DTEND"]) if "DTEND" in props else None
    get = lambda key: _text(props.get(key, ("", ""))[1])  # noqa: E731
    conference = get("X-GOOGLE-CONFERENCE").strip()
    url = conference if _MEET_URL.fullmatch(conference) else None
    url = url or next((u.group(0) for u in (_MEET_URL.search(get(k)) for k in ("LOCATION", "DESCRIPTION")) if u), None)
    cancelled = method == "CANCEL" or get("STATUS").strip().upper() == "CANCELLED"
    return Invite(
        uid=uid,
        sequence=int(get("SEQUENCE").strip() or 0),
        method="CANCEL" if cancelled else "REQUEST",
        start=start,
        end=end,
        summary=get("SUMMARY").strip(),
        description=get("DESCRIPTION"),
        organizer_email=_email(props.get("ORGANIZER", ("", ""))[1]),
        attendee_emails=attendees[:100],
        meeting_url=url or None,
    )


async def team_row(owner_id: str, address: str) -> dict | None:
    """The CEO's own address row re-pointed at the team address, so team mail is gated, stored,
    capped and delivered exactly like mail to the CEO. None when the workspace has no CEO."""
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    ceo = (getattr(row, "starter_agent_ids", None) or {}).get("ceo")
    if not ceo:
        return None
    own = next(
        (
            r
            for r in await agent_email_repo.query_by_owner(owner_id)
            if r.get("agent_id") == ceo and r.get("status") == "active"
        ),
        {"send_to_anyone": True},
    )
    return {**own, "address": address, "owner_id": owner_id, "agent_id": ceo}


async def handle_team_mail(owner_id: str, payload, team: dict) -> str | None:
    """A member's invite becomes (or updates, or cancels) a meeting row and returns the outcome.
    None means "this is mail": no calendar part, an unreadable one, or a stranger's invite."""
    if not getattr(payload, "calendar", None):
        return None
    # The ICS organizer and attendees are sender-written; only the authenticated From decides,
    # and only a member's calendar part is parsed at all.
    sender = parse_from_address(payload.from_)
    if not authenticated_sender(payload.verdicts or {}, payload.headers or {}, sender):
        return None
    members = {str(m["email"]).lower() for m in await clerk_admin.list_organization_members(owner_id) if m.get("email")}
    if not sender or not (sender in members or sender_allowed(team, sender)):
        return None  # a stranger's mail: no bot, no room -- the CEO reads it as mail
    inviter = sender
    invite = parse(payload.calendar)
    if invite is None or (invite.method == "REQUEST" and not invite.meeting_url):
        return None  # not a Meet invite we can join: the CEO reads it as mail
    existing = await meetings_repo.get_meeting(owner_id, invite.uid)
    if existing and int(existing.get("sequence", 0)) > invite.sequence:
        return "meeting_stale"
    if invite.method == "CANCEL":
        if not existing or existing.get("organizer") != invite.organizer_email:
            return "meeting_ignored"
        await meetings_repo.set_status(owner_id, invite.uid, "cancelled", sequence=invite.sequence)
        return "meeting_cancelled"
    status = existing.get("status") if existing and existing.get("status") != "cancelled" else "scheduled"
    await meetings_repo.put_meeting(
        {
            **(existing or {}),
            "ownerId": owner_id,
            "uid": invite.uid,
            "sequence": invite.sequence,
            "title": invite.summary,
            "startAt": int(invite.start.timestamp()),
            "endAt": int(invite.end.timestamp()) if invite.end else None,
            "meetingUrl": invite.meeting_url,
            "organizer": invite.organizer_email,
            "attendees": invite.attendee_emails,
            "status": status,
            "createdBy": (existing or {}).get("createdBy") or inviter,
        }
    )
    logger.info("meeting %s from invite: url …%s", invite.uid, (invite.meeting_url or "")[-4:])
    return "meeting_scheduled"
