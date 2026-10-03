"""Calendar invites to the team address ``agents@{slug}``: parse the ``text/calendar`` part and
turn a workspace member's invite into a meeting room. Everything else -- plain team mail and a
stranger's invite alike -- is ordinary team mail, delivered by the inbound route.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from core.constants import SYSTEM_ACTOR_ID
from core.repositories import container_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin, meeting_rsvp, messaging
from core.services.agent_mail_inbound import authenticated_sender, parse_from_address

logger = logging.getLogger(__name__)

MEETING_LEASE_S = 60


class MeetingBusy(Exception):
    """Another delivery holds this meeting's lease: the SES route 503s and SES redelivers."""


_MEET_URL = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")
_LINE = re.compile(r'^((?:[^:"]|"[^"]*")*):(.*)$')  # name;params:value -- a quoted param may hold ':'


@dataclass(frozen=True)
class Invite:
    uid: str
    sequence: int
    method: str  # "REQUEST" | "CANCEL"
    start: datetime | None  # None only on a CANCEL that carried no DTSTART (RFC 5546 §3.2.5)
    end: datetime | None
    summary: str
    description: str
    organizer_email: str | None
    attendee_emails: list[str]
    meeting_url: str | None


def _text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _email(value: str) -> str:
    return re.sub(r"(?i)^mailto:", "", value).strip().lower()


def _organizer(value: str) -> str | None:
    """One bare address, or None: a list would put many recipients on the one RSVP."""
    email = _email(value)
    return None if re.search(r"[,;<>\s]", email) else email


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
    if method not in ("REQUEST", "CANCEL"):
        return None  # PUBLISH/COUNTER/REFRESH/REPLY are not invites
    get = lambda key: _text(props.get(key, ("", ""))[1])  # noqa: E731
    cancelled = method == "CANCEL" or get("STATUS").strip().upper() == "CANCELLED"
    if not uid or len(uid) > 512 or "RECURRENCE-ID" in props or not ("DTSTART" in props or cancelled):
        return None
    start = _when(*props["DTSTART"]) if "DTSTART" in props else None
    end = _when(*props["DTEND"]) if "DTEND" in props else None
    conference = get("X-GOOGLE-CONFERENCE").strip()
    url = conference if _MEET_URL.fullmatch(conference) else None
    url = url or next((u.group(0) for u in (_MEET_URL.search(get(k)) for k in ("LOCATION", "DESCRIPTION")) if u), None)
    return Invite(
        uid=uid,
        sequence=int(get("SEQUENCE").strip() or 0),
        method="CANCEL" if cancelled else "REQUEST",
        start=start,
        end=end,
        summary=get("SUMMARY").strip(),
        description=get("DESCRIPTION"),
        organizer_email=_organizer(props.get("ORGANIZER", ("", ""))[1]),
        attendee_emails=attendees[:100],
        meeting_url=url or None,
    )


def _when_text(start: datetime) -> str:
    """``Fri Oct 3, 3:00 PM EDT`` in the invite's own zone."""
    return f"{start:%a %b %-d, %-I:%M %p %Z}"


async def _post_line(owner_id: str, room_id: str, key: str, text: str) -> None:
    """One system line. The key is the receipt: an SES retry, or a resend whose wording differs
    under the same key, replays it instead of doubling it or raising."""
    try:
        await messaging_repo.create_message(
            room_id=room_id,
            owner_id=owner_id,
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=messaging_repo.human_idemp_sk(SYSTEM_ACTOR_ID, key),
            req_hash=messaging_repo.request_hash(key),
            author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
        )
    except (messaging_repo.IdempotencyConflict, messaging_repo.RoomArchived, messaging_repo.RoomNotFound):
        pass  # replayed, or a member archived/deleted the room since: nothing left to announce


async def _create_room(owner_id: str, room_id: str, invite: Invite, creator: str, by_email: dict, meeting: dict):
    """The meeting's channel: the inviting member owns it, other member attendees join, and every
    starter agent listens on ``mentions``. A replay or a conflicting create returns quietly; the
    caller's update path takes it from there."""
    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    starter = set((getattr(company, "starter_agent_ids", None) or {}).values())
    roster = [a for a in await messaging.load_agent_roster(owner_id) if a["id"] in starter]
    handles = messaging.mention_handles(roster)
    others = [by_email[e] for e in dict.fromkeys(invite.attendee_emails) if by_email.get(e) not in (None, creator)]
    humans = [creator, *others][: messaging_repo.MAX_ROOM_MEMBERS - len(roster)]
    members = [
        {
            "kind": "human",
            "id": user_id,
            "displayName": await messaging.human_display_name(user_id),
            "mentionHandle": None,
            "role": "owner" if user_id == creator else "member",
            "listenMode": None,
        }
        for user_id in humans
    ] + [
        {
            "kind": "agent",
            "id": a["id"],
            "displayName": a["name"],
            "mentionHandle": handles[a["id"]],
            "role": "member",
            "listenMode": "mentions",
        }
        for a in roster
    ]
    for user_id in humans:
        await messaging_repo.ensure_user_stream(owner_id, user_id)
    name = f"{(invite.summary or 'Meeting')[:60]} — {invite.start:%b %-d}"
    normalized = [{k: m.get(k) for k in ("kind", "id", "role", "listenMode")} for m in members]
    try:
        await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"meeting:{invite.uid}",
            creator_user_id=creator,
            name=name,
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash(name, "channel", normalized),
            extra={"meeting": meeting},
        )
    except messaging_repo.MessagingRepoError:
        # A replay with a different body, or a second member's forward racing this one (its META
        # Put cancels with no receipt of ours): the room is someone's, so update it. A cancel that
        # left no room was transient: raise, so the route 503s and SES redelivers.
        if await messaging_repo.get_room(room_id) is None:
            raise


async def _newer_stored(room_id: str, sequence) -> bool:
    """True when the room META already holds a later invite's sequence: an older delivery that read
    the room before the newer one committed must not overwrite it."""
    newer = ((await messaging_repo.get_room(room_id) or {}).get("meeting") or {}).get("sequence", 0)
    if int(newer) > int(sequence):
        logger.info("meeting room %s: a newer invite (sequence %s) is already stored", room_id, newer)
        return True
    return False


async def _patch_meeting(room_id: str, meeting: dict) -> bool:
    """``patch_room_meta`` under its revision CAS, retried once against a concurrent META edit
    (a rename). Losing twice raises: the route 503s and SES redelivers. False when a newer
    sequence is stored, so nothing was written."""
    for attempt in (1, 2):
        if await _newer_stored(room_id, meeting.get("sequence", 0)):
            return False
        try:
            await messaging_repo.patch_room_meta(room_id, {"meeting": meeting})
            return True
        except ClientError as exc:
            # a throttle or access error, or the revision race lost twice: SES retries the mail
            if attempt == 2 or not messaging_repo._is_cancelled(exc):
                raise
    raise AssertionError("unreachable")


async def handle_team_mail(owner_id: str, payload, team: dict) -> str | None:
    """A member's invite creates (or updates, or cancels) the meeting's room -- the room META's
    ``meeting`` is the record -- and returns the outcome. None means "this is mail": no calendar
    part, an unreadable one, or a stranger's invite."""
    if not getattr(payload, "calendar", None):
        return None
    # The ICS organizer and attendees are sender-written; only the authenticated From decides,
    # and only a member's calendar part is parsed at all.
    sender = parse_from_address(payload.from_)
    if not authenticated_sender(payload.verdicts or {}, payload.headers or {}, sender):
        return None
    by_email = {
        str(m["email"]).lower(): m["user_id"]
        # strict: a Clerk blip must 503 (SES retries), not read the member as a stranger forever
        for m in await clerk_admin.list_organization_members(owner_id, strict=True)
        if m.get("email")
    }
    creator = by_email.get(sender) if sender else None
    if creator is None:
        return None  # a stranger's mail: no bot, no room -- the CEO reads it as mail
    invite = parse(payload.calendar)
    if invite is None or (invite.method == "REQUEST" and not invite.meeting_url):
        return None  # not a Meet invite we can join: the CEO reads it as mail
    room_id = messaging_repo.meeting_room_id(owner_id, invite.uid)
    # One lease per meeting serializes everything below (sequence check, line, META, RSVP)
    # across concurrent deliveries; the loser 503s and SES redelivers after the holder is done.
    lease = f"meeting#{room_id}"
    token = await container_repo.try_claim_owner_lifecycle(lease, stale_s=MEETING_LEASE_S)
    if not token:
        raise MeetingBusy(room_id)
    try:
        return await _apply(owner_id, invite, room_id, creator, by_email, team)
    finally:
        await container_repo.release_owner_lifecycle_claim(lease, token=token)


async def _apply(owner_id: str, invite: Invite, room_id: str, creator: str, by_email: dict, team: dict) -> str | None:
    room = await messaging_repo.get_room(room_id) or {}
    if room.get("archivedAt") or room.get("deletedAt"):
        return "meeting_ignored"  # a member closed this meeting's room; a move/cancel must not reopen it
    current = room.get("meeting")
    if current and int(current.get("sequence", 0)) > invite.sequence:
        return "meeting_stale"
    if invite.method == "CANCEL":
        if not current or current.get("organizer") != invite.organizer_email:
            return "meeting_ignored"
        start_at = int(current.get("startAt", 0))  # a cancellation need not carry DTSTART
        meeting = {**current, "status": "cancelled", "sequence": invite.sequence}
        text = "This meeting was cancelled."
    else:
        start_at = int(invite.start.timestamp())
        meeting = {
            **(current or {}),
            "uid": invite.uid,
            "sequence": invite.sequence,
            "title": invite.summary or "Meeting",
            "startAt": start_at,
            "endAt": int(invite.end.timestamp()) if invite.end else None,
            "meetingUrl": invite.meeting_url,
            "organizer": invite.organizer_email,
            "status": "scheduled" if not current or current.get("status") == "cancelled" else current["status"],
        }
        moved = current and current.get("status") != "cancelled" and int(current.get("startAt", 0)) != start_at
        text = (
            f"**{meeting['title']}** moved to {_when_text(invite.start)}."
            if moved
            else f"Agents will join **{meeting['title']}** on {_when_text(invite.start)}."
        )
    # A new room counts as coming from "scheduled", so a retry after the create replays its line;
    # cancelled -> scheduled is a new key, so a same-sequence re-invite announces itself again.
    prev_status = (current or {}).get("status", "scheduled")
    if current is None:
        await _create_room(owner_id, room_id, invite, creator, by_email, meeting)
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            return None  # no room came of it: the CEO reads it as mail
        current = meta.get("meeting")
    elif await _newer_stored(room_id, invite.sequence):
        return "meeting_stale"  # a newer invite committed after the read above
    # Line first, then META: a failure between them retries into the same (replayed) line.
    await _post_line(
        owner_id, room_id, f"meeting:{invite.uid}:{invite.sequence}:{start_at}:{prev_status}>{meeting['status']}", text
    )
    if meeting != current and not await _patch_meeting(room_id, meeting):
        return "meeting_stale"
    logger.info("meeting room %s: %s", room_id, meeting["status"])
    if invite.method == "CANCEL":
        return "meeting_cancelled"
    if (
        not team.get("outbound_disabled")
        and meeting.get("rsvpSequence") != invite.sequence
        and await meeting_rsvp.accept(owner_id, invite)
    ):
        await _patch_meeting(room_id, {**meeting, "rsvpSequence": invite.sequence})
    return "meeting_scheduled"
