"""Calendar invites to the team address ``agents@{slug}``: parse the ``text/calendar`` part and
turn a workspace member's invite into a meeting room. Everything else -- plain team mail and a
stranger's invite alike -- is ordinary team mail, delivered by the inbound route.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from core.config import settings
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin, meeting_bots, meeting_rsvp, messaging
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
    if method not in ("REQUEST", "CANCEL"):
        return None  # PUBLISH/COUNTER/REFRESH/REPLY are not invites
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


def _when_text(start: datetime) -> str:
    """``Fri Oct 3, 3:00 PM EDT`` in the invite's own zone."""
    return f"{start:%a %b %-d, %-I:%M %p %Z}"


async def _post_line(
    owner_id: str, room_id: str, key: str, text: str, *, attachments: list | None = None, dispatch_targets: list = ()
) -> dict | None:
    """One system line, returned (None when the room is gone). The key is the receipt: an SES
    retry, or a resend whose wording differs under the same key, replays it instead of doubling
    it or raising."""
    try:
        message, _ = await messaging_repo.create_message(
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
            dispatch_targets=list(dispatch_targets),
            source_agent_id=None,
            client_message_id=None,
            attachments=attachments,
        )
        return message
    except (messaging_repo.IdempotencyConflict, messaging_repo.RoomArchived, messaging_repo.RoomNotFound):
        return None  # replayed, or a member archived/deleted the room since: nothing left to announce


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
        # Put cancels with no receipt of ours): either way the room is someone's, so update it.
        pass


async def _patch_meeting(room_id: str, fields: dict, drop: tuple = ()) -> dict | None:
    """THE one writer of the room META's ``meeting`` map: re-read it, lay only the caller's own
    fields over it (dropping ``drop``), and write under the revision CAS -- so the invite path, the
    bot path and the RSVP never erase each other's keys. A write carrying an older ``sequence`` than
    the stored one writes nothing and returns None (checked against a fresh read before every attempt).
    Three lost races raise, so SES redelivers. Returns the map as written."""
    for attempt in (1, 2, 3):
        stored = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if "sequence" in fields and int(stored.get("sequence", 0)) > int(fields["sequence"]):
            logger.info("meeting room %s: a newer invite (sequence %s) won the race", room_id, stored["sequence"])
            return None
        merged = {k: v for k, v in {**stored, **fields}.items() if k not in drop}
        if merged == stored:
            return stored
        try:
            await messaging_repo.patch_room_meta(room_id, {"meeting": merged})
            return merged
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 3:
                raise  # a throttle, access error, or a third lost race: SES retries the whole mail


_BOT_KEYS = ("recallBotId", "ticketNonce", "botClaimAt", "botError")
_CLAIM_S = 60  # a claimed-but-unwritten booking younger than this is another delivery's POST in flight


def _code(exc: Exception) -> str:
    code = exc.code if isinstance(exc, meeting_bots.RecallError) else "error"
    put_metric("meeting.bot_failed", dimensions={"code": code})
    return code


def _definitive(exc: Exception) -> bool:
    """A 4xx Recall answered (not 409/429): no bot was booked, so a retry may mint a new nonce.
    A timeout, network error, 5xx, 429 or unreadable answer may have booked one: keep the nonce."""
    code = getattr(exc, "code", "")
    return code.isdigit() and 400 <= int(code) < 500 and code not in ("409", "429")


async def _bot_failed(owner_id: str, room_id: str, meeting: dict, exc: Exception, verb: str, keep: bool) -> dict:
    """``bot_failed`` + one line + one metric. ``keep`` holds on to a bot (and nonce) that may exist."""
    code = _code(exc)
    logger.warning("meeting room %s: bot %s failed (%s)", room_id, verb, code)
    if verb == "book":
        stored = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if stored.get("recallBotId"):
            return stored  # another delivery booked it: nothing failed
    reason = exc.reason if isinstance(exc, meeting_bots.RecallError) else "something went wrong"
    await _post_line(
        owner_id,
        room_id,
        f"meeting:{meeting['uid']}:{meeting['sequence']}:{meeting['startAt']}:{verb}_failed:{code}",
        f"Couldn't {verb} a bot for this meeting: {reason}. Forward the invite again to retry.",
    )
    return await _patch_meeting(
        room_id, {"status": "bot_failed", "botError": code}, drop=() if keep else ("ticketNonce", "botClaimAt")
    )


async def _sync_bot(owner_id: str, room_id: str, meeting: dict, prior: dict | None) -> dict:
    """Books, moves or pulls the meeting's Recall bot and returns the meeting as written. A Recall
    failure never raises into the mail route; a lost META race past three tries does (SES retries)."""
    bot_id = meeting.get("recallBotId")
    if meeting["status"] == "cancelled":
        if not bot_id:
            return meeting
        try:
            await meeting_bots.leave(bot_id)
        except Exception as exc:  # noqa: BLE001 -- the id stays, so a later CANCEL retries
            code = _code(exc)
            logger.warning("meeting room %s: bot leave failed (%s)", room_id, code)
            await _post_line(
                owner_id,
                room_id,
                f"meeting:{meeting['uid']}:{meeting['sequence']}:leave_failed",
                "Couldn't pull the bot out of this meeting.",
            )
            return await _patch_meeting(room_id, {"botError": code})
        return await _patch_meeting(room_id, {}, drop=_BOT_KEYS)
    if bot_id and meeting["status"] != "bot_failed" and prior and prior.get("meetingUrl") == meeting["meetingUrl"]:
        if int(prior.get("startAt", 0)) == meeting["startAt"]:
            return meeting
        try:
            await meeting_bots.reschedule(bot_id, room_id=room_id, meeting=meeting)
            return meeting
        except Exception as exc:  # noqa: BLE001
            if not (isinstance(exc, meeting_bots.RecallError) and exc.code == "400"):
                return await _bot_failed(owner_id, room_id, meeting, exc, "move", keep=True)
            # 400: under 10 minutes out Recall wants a fresh bot -- replaced below
    if not settings.meetings_available():
        logger.info("meeting room %s: meetings unavailable here, no bot booked", room_id)
        return meeting
    if bot_id:  # a new time, a new link, or a failed move: the old bot goes before a new one comes
        try:
            await meeting_bots.leave(bot_id)
        except Exception as exc:  # noqa: BLE001 -- the old bot stands, so does its id
            code = _code(exc)
            logger.warning("meeting room %s: bot leave failed (%s)", room_id, code)
            await _post_line(
                owner_id,
                room_id,
                f"meeting:{meeting['uid']}:{meeting['sequence']}:{meeting['startAt']}:replace_failed",
                "Couldn't move the bot; it will join at the old time.",
            )
            return await _patch_meeting(room_id, {"status": "bot_failed", "botError": code})
        meeting = await _patch_meeting(room_id, {"status": "scheduled"}, drop=_BOT_KEYS)
    # One booking per meeting: the nonce is claimed with a conditional write. A delivery that loses
    # the claim POSTs only to retry a failed booking or to recover a claim whose POST never reported
    # back; the stored nonce rides as Recall's Idempotency-Key either way.
    nonce, now = secrets.token_urlsafe(32), int(time.time())
    if not await messaging_repo.set_meeting_field_if_absent(room_id, "ticketNonce", nonce, {"botClaimAt": now}):
        stored = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if stored.get("recallBotId"):
            return stored
        if stored.get("status") != "bot_failed" and now - int(stored.get("botClaimAt", 0)) < _CLAIM_S:
            return stored  # the claimant's POST is in flight; it writes the bot
        nonce = stored.get("ticketNonce") or nonce
    org = await clerk_admin.get_organization(owner_id) or {}
    try:
        bot_id = await meeting_bots.create(
            room_id=room_id, meeting={**meeting, "ticketNonce": nonce}, company=org.get("name") or "Your team"
        )
    except meeting_bots.RecallError as exc:
        if exc.code == "409":  # the same key is in flight from the other delivery: it writes the bot
            return meeting
        return await _bot_failed(owner_id, room_id, meeting, exc, "book", keep=not _definitive(exc))
    except Exception as exc:  # noqa: BLE001
        return await _bot_failed(owner_id, room_id, meeting, exc, "book", keep=True)
    booked = {"recallBotId": bot_id, "ticketNonce": nonce}
    stored = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
    if stored.get("status") != "cancelled":
        booked["status"] = "bot_created"
    written = await _patch_meeting(room_id, booked, drop=("botError", "botClaimAt"))
    if stored.get("status") == "cancelled" or (stored.get("startAt"), stored.get("meetingUrl")) != (
        meeting["startAt"],
        meeting["meetingUrl"],
    ):
        # A CANCEL or a move landed while the POST was in flight and found no bot to act on:
        # act on it now, against what is stored, with this bot.
        return await _sync_bot(owner_id, room_id, written, meeting)
    return written


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
    room = await messaging_repo.get_room(room_id) or {}
    if room.get("archivedAt") or room.get("deletedAt"):
        return "meeting_ignored"  # a member closed this meeting's room; a move/cancel must not reopen it
    current = room.get("meeting")
    if current and int(current.get("sequence", 0)) > invite.sequence:
        return "meeting_stale"
    start_at = int(invite.start.timestamp())
    allowed = settings.meetings_available()
    if invite.method == "CANCEL":
        if not current or current.get("organizer") != invite.organizer_email:
            return "meeting_ignored"
        fields = {"status": "cancelled", "sequence": invite.sequence}
        text = "This meeting was cancelled."
    else:
        # The invite owns these keys only; status, bot and RSVP keys belong to their own writers.
        fields = {
            "uid": invite.uid,
            "sequence": invite.sequence,
            "title": invite.summary or "Meeting",
            "startAt": start_at,
            "endAt": int(invite.end.timestamp()) if invite.end else None,
            "meetingUrl": invite.meeting_url,
            "organizer": invite.organizer_email,
        }
        moved = current and current.get("status") != "cancelled" and int(current.get("startAt", 0)) != start_at
        text = (
            f"**{fields['title']}** moved to {_when_text(invite.start)}."
            if moved
            else f"Agents will join **{fields['title']}** on {_when_text(invite.start)}."
        )
        if not allowed:  # the room still keeps the meeting's notes; no bot books, no RSVP goes out
            text = "Meetings aren't available in this environment yet — the room is here for notes."
    # The line's key is the invite's own transition: a new room counts as coming from "scheduled",
    # so a retry after the create replays its line; cancelled -> scheduled is a new key, so a
    # same-sequence re-invite announces itself again.
    before = "cancelled" if (current or {}).get("status") == "cancelled" else "scheduled"
    after = "cancelled" if invite.method == "CANCEL" else "scheduled"
    if current is None:
        await _create_room(owner_id, room_id, invite, creator, by_email, {**fields, "status": "scheduled"})
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            return None  # no room came of it: the CEO reads it as mail
        current = meta.get("meeting")  # ours, or a racing delivery's
    elif (
        int((((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}).get("sequence", 0))
        > invite.sequence
    ):
        return "meeting_stale"  # a newer invite committed after the read above: no line, no bot, no RSVP
    if invite.method == "REQUEST" and (not current or current.get("status") == "cancelled"):
        fields["status"] = "scheduled"
    # Line first, then META: a failure between them retries into the same (replayed) line.
    await _post_line(owner_id, room_id, f"meeting:{invite.uid}:{invite.sequence}:{start_at}:{before}>{after}", text)
    meeting = await _patch_meeting(room_id, fields)
    if meeting is None:
        return "meeting_stale"  # a newer invite landed meanwhile: it owns the bot and the RSVP
    meeting = await _sync_bot(owner_id, room_id, meeting, current)
    logger.info("meeting room %s: %s", room_id, meeting["status"])
    if invite.method == "CANCEL":
        return "meeting_cancelled"
    if (
        allowed
        and not team.get("outbound_disabled")
        and meeting.get("rsvpSequence") != invite.sequence
        and await meeting_rsvp.accept(owner_id, invite)
    ):
        await _patch_meeting(room_id, {"rsvpSequence": invite.sequence})
    return "meeting_scheduled"
