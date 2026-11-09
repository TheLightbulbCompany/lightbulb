"""Calendar invites to the team address ``agents@{slug}``: parse the ``text/calendar`` part and
turn a workspace member's invite into a meeting room. Everything else -- plain team mail and a
stranger's invite alike -- is ordinary team mail, delivered by the inbound route.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError
from dateutil.rrule import DAILY, rruleset, rrulestr

from core.config import settings
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, container_repo, messaging_repo
from core.services import agent_email, clerk_admin, meeting_bots, meeting_rsvp, messaging
from core.services.agent_mail_inbound import authenticated_sender, parse_from_address

logger = logging.getLogger(__name__)

MEETING_LEASE_S = 60


class MeetingBusy(Exception):
    """Another transition holds this meeting's lease: the caller answers 5xx and is redelivered."""


@asynccontextmanager
async def meeting_lease(room_id: str, *, tries: int = 1):
    """THE one lease every meeting lifecycle transition runs under -- an invite, a Recall status,
    the worker's idle leave -- so each decides from a fresh META read nobody else is changing.
    ``tries`` > 1 retries only the acquisition, 2 s apart."""
    lease = f"meeting#{room_id}"
    token = None
    for attempt in range(tries):
        if attempt:
            await asyncio.sleep(2)
        if token := await container_repo.try_claim_owner_lifecycle(lease, stale_s=MEETING_LEASE_S):
            break
    if not token:
        raise MeetingBusy(room_id)
    try:
        yield
    finally:
        await container_repo.release_owner_lifecycle_claim(lease, token=token)


_MAX_SERIES_AGE_S = 20 * 365 * 86400
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
    # A series: {"rule", "first", "tz", "exdates", "duration"}; start/end are then its next occurrence.
    recurrence: dict | None = None
    instance_of: str | None = None  # the series UID of a lone RECURRENCE-ID event (uid is then "<UID>#<id>")
    recurrence_id_line: str | None = None  # that event's RECURRENCE-ID property as received, for the RSVP


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
    tzid = re.search(r'TZID="?([^";:]+)', params, re.IGNORECASE)
    # ponytail: Windows TZIDs (Outlook) unsupported → invite stays mail; read the VTIMEZONE offsets when an Outlook+Meet customer appears.
    zone = ZoneInfo(tzid.group(1)) if tzid else timezone.utc
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone)


def parse(calendar_text: str) -> Invite | None:
    """The event of an iCalendar body (a series' master, else the first VEVENT), or None for
    anything it cannot use: no VEVENT, no UID, an oversized UID, a series with no occurrence left,
    or any unreadable value. Total: it never raises, so a malformed invite is mail, never a 5xx."""
    try:
        return _parse(calendar_text)
    except Exception:  # noqa: BLE001 -- sender-written input; no content in the log
        logger.debug("meeting invite: unparseable calendar part")
        return None


def next_occurrence(recurrence: dict, after: float) -> tuple[datetime, datetime] | None:
    """The first occurrence of a series whose END is after ``after`` (epoch s), as zone-aware
    (start, end), or None. The rule runs from a zone-aware first start, so it keeps its wall clock
    across DST."""
    zone = ZoneInfo(recurrence["tz"])
    duration = int(recurrence["duration"])
    rules = rruleset()
    rules.rrule(rrulestr(recurrence["rule"], dtstart=datetime.fromtimestamp(int(recurrence["first"]), zone)))
    for x in recurrence["exdates"]:
        rules.exdate(datetime.fromtimestamp(int(x), zone))
    start = rules.after(datetime.fromtimestamp(after - duration, zone))
    return (start, datetime.fromtimestamp(start.timestamp() + duration, zone)) if start else None


def _parse(calendar_text: str) -> Invite | None:
    text = re.sub(r"\n[ \t]", "", (calendar_text or "").replace("\r\n", "\n"))
    method, stack, events = "REQUEST", [], []  # events: (props, attendees, exdates) per VEVENT
    for line in text.split("\n"):
        m = _LINE.match(line)
        if not m:
            continue
        head, value = m.groups()
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                events.append(({}, [], []))
        elif name == "END":
            if stack:
                stack.pop()
        elif name == "METHOD" and not stack[1:]:
            method = value.strip().upper()
        elif stack[-1:] == ["VEVENT"]:  # skips VALARM/VTIMEZONE, which carry their own DESCRIPTION/DTSTART
            props, attendees, exdates = events[-1]
            if name == "ATTENDEE":
                attendees.append(_email(value))
            elif name == "EXDATE":
                exdates.extend((params, v) for v in value.split(","))
            props.setdefault(name, (params, value))
    if method not in ("REQUEST", "CANCEL") or not events:
        return None  # PUBLISH/COUNTER/REFRESH/REPLY are not invites
    props, attendees, exdates = next((e for e in events if "RECURRENCE-ID" not in e[0]), events[0])
    get = lambda key: _text(props.get(key, ("", ""))[1])  # noqa: E731
    cancelled = method == "CANCEL" or get("STATUS").strip().upper() == "CANCELLED"
    uid, instance_of, rid_line = props.get("UID", ("", ""))[1].strip(), None, None
    if uid and "RECURRENCE-ID" in props:  # one occurrence on its own: a one-off meeting of its own
        rid_params, rid = props["RECURRENCE-ID"]
        original = int(_when(rid_params, rid).timestamp())
        instance_of, uid = uid, f"{uid}#{original}"  # the same instant has one identity across timezones
        rid_line = f"RECURRENCE-ID{';' + rid_params if rid_params else ''}:{rid.strip()}"
    if not uid or len(uid) > 512 or not ("DTSTART" in props or cancelled):
        return None
    start = _when(*props["DTSTART"]) if "DTSTART" in props else None
    end = _when(*props["DTEND"]) if "DTEND" in props else None
    recurrence = None
    if "RRULE" in props and start and not instance_of and not cancelled:  # a CANCEL acts on the stored meeting
        rule, now = props["RRULE"][1].strip(), time.time()
        # Bounds on a sender-written rule: the walk from its first start must stay short.
        if (
            len(rule) > 512
            or len(re.findall(r"(?i)(?:^|;)FREQ=", rule)) != 1  # a second FREQ would win in dateutil
            or not re.search(r"(?i)(^|;)FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;|$)", rule)
            or rrulestr(rule, dtstart=start)._freq > DAILY  # and what dateutil resolved agrees
            or start.timestamp() < now - _MAX_SERIES_AGE_S
        ):
            return None
        duration = min(max(int((end - start).total_seconds()) if end else 0, 0), 86400)
        recurrence = {
            "rule": rule,
            "first": int(start.timestamp()),
            "tz": getattr(start.tzinfo, "key", "UTC"),
            # From the occurrence in progress on: its exclusion still matters until it ends.
            "exdates": sorted(t for t in (int(_when(p, v).timestamp()) for p, v in exdates) if t >= now - duration)[
                :200
            ],
            "duration": duration,
        }
        nxt = next_occurrence(recurrence, now)
        if nxt is None:  # the series is over (a "this and following" edit cut it): release its bot
            cancelled, recurrence = True, None
            start = end = None
        else:
            start, end = nxt
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
        recurrence=recurrence,
        instance_of=instance_of,
        recurrence_id_line=rid_line,
    )


def _when_text(start: datetime) -> str:
    """``Fri Oct 3, 3:00 PM EDT`` in the invite's own zone."""
    return f"{start:%a %b %-d, %-I:%M %p %Z}"


async def output_room(owner_id: str, room_id: str) -> str:
    """Where a meeting's lines go (its transcript, the notes, booking notices): the workspace's
    default room while it stands; else the organizer's DM with the agent who writes the notes, so a
    workspace without a default room still sees them (10-09: the founder's notes sat in the hidden
    meeting room, which no sidebar shows); else the meeting's own room (nothing is lost)."""
    default = messaging.default_room_id(owner_id)
    meta = await messaging_repo.get_room(default)
    if meta and not meta.get("archivedAt") and not meta.get("deletedAt"):
        return default
    return await _organizer_dm(owner_id, room_id) or room_id


async def _organizer_dm(owner_id: str, room_id: str) -> str | None:
    """The DM between the meeting's organizer (the member whose invite made its room, seated as its
    owner) and the agent that writes its notes (the CEO when it sits in the meeting, as the notes
    writer is picked). Found or created; None when either is missing or it cannot be opened. A DM
    passed in (a line already resolved here) resolves to itself."""
    from core.auth import AuthContext
    from core.services import openclaw_work

    members = await messaging_repo.list_members(room_id)
    creator = next((m["id"] for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)
    agents = sorted(m["id"] for m in members if m.get("kind") == "agent")
    if not creator or not agents:
        return None
    writer = await openclaw_work._fallback_single(owner_id, agents, {}, [])
    request = messaging.CreateRoomRequest(
        client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-meeting-dm|{owner_id}|{creator}|{writer}"),
        name=None,
        kind="dm",
        members=[messaging.CreateRoomMember(kind="agent", id=writer, role="member", listen_mode="active")],
    )
    try:
        room = await messaging.create_room(AuthContext(user_id=creator, org_id=owner_id), request)
    except messaging.ApiProblem:
        logger.warning("meeting room %s: could not open the organizer's DM", room_id, exc_info=True)
        return None
    return str(room.room_id)


async def _post_line(
    owner_id: str, room_id: str, key: str, text: str, *, attachments: list | None = None, dispatch_targets: list = ()
) -> dict | None:
    """One system line about the meeting ``room_id``, posted to its ``output_room`` and returned
    (None when that room is gone). The key is the receipt: an SES retry, or a resend whose wording
    differs under the same key, replays it instead of doubling it or raising. Keys carry the
    meeting's identity, so meetings sharing the default room never collide."""
    room_id = await output_room(owner_id, room_id)
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


async def _create_room(owner_id: str, room_id: str, invite: Invite, creator: str, meeting: dict):
    """The meeting's room: hidden from every room list, owned by the inviting member (the one human,
    whom the Meet bot's speech acts as), with every agent of the workspace listening on ``mentions``.
    A replay or a conflicting create returns quietly; the caller's update path takes it from there."""
    # ponytail: the room member cap bounds a meeting to the first N agents; read the roster directly in voice_calls.room_agents when a workspace outgrows it.
    roster = (await messaging.load_agent_roster(owner_id))[: messaging_repo.MAX_ROOM_MEMBERS - 1]
    handles = messaging.mention_handles(roster)
    members = [
        {
            "kind": "human",
            "id": creator,
            "displayName": await messaging.human_display_name(creator),
            "mentionHandle": None,
            "role": "owner",
            "listenMode": None,
        }
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
    await messaging_repo.ensure_user_stream(owner_id, creator)
    name = (invite.summary or "Meeting")[:60]  # every voice prompt names the call by it
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
            extra={"meeting": meeting, "hidden": True},
        )
    except messaging_repo.MessagingRepoError:
        # A replay with a different body, or a second member's forward racing this one (its META
        # Put cancels with no receipt of ours): the room is someone's, so update it. A cancel that
        # left no room was transient: raise, so the route 503s and SES redelivers.
        if await messaging_repo.get_room(room_id) is None:
            raise


async def adopted_audio(room_id: str, bot_id: str) -> str | None:
    """An adopted bot may be older than per-person audio (a deploy between its create and now): the
    record follows what the bot really sends. Unknown reads as the mixed stream, which the bridge refuses."""
    try:
        return await meeting_bots.audio_mode(bot_id)
    except Exception:  # noqa: BLE001
        logger.warning("meeting room %s: adopted bot's audio mode unknown, assuming mixed", room_id)
        return None


async def _patch_meeting(
    room_id: str, fields, drop: tuple = (), when=None, *, keep_transcript: bool = False
) -> dict | None:
    """THE one writer of the room META's ``meeting`` map: re-read it, lay only the caller's own
    fields over it (dropping ``drop``), and write under the revision CAS -- so the invite path, the
    bot path and the RSVP never erase each other's keys. A write carrying an older ``sequence`` than
    the stored one writes nothing and returns None (checked against a fresh read before every attempt).
    The write is bound to the revision that was READ, so a writer in between (a webhook's ``ended``
    under a transcript write) cancels it and the merge is redone over the new state.
    ``when`` is a predicate on the stored map, checked on every attempt: False writes nothing and
    returns None (a marker meant for one occurrence never lands on the next).
    ``keep_transcript`` moves the stored occurrence's transcript into ``history`` before ``drop``,
    from the map read on each attempt (a transcript written after the caller's read is kept).
    ``fields`` may be a function of the stored map, called on every attempt: a read-modify-write
    (the roster, the unsaid answers) that a racing writer cannot undo.
    Three lost races raise, so SES redelivers. Returns the map as written."""
    for attempt in (1, 2, 3):
        meta = (await messaging_repo.get_room(room_id)) or {}
        stored = meta.get("meeting") or {}
        if when is not None and not when(stored):
            return None
        own = fields(stored) if callable(fields) else fields
        if "sequence" in own and int(stored.get("sequence", 0)) > int(own["sequence"]):
            logger.info("meeting room %s: a newer invite (sequence %s) won the race", room_id, stored["sequence"])
            return None
        kept = _kept_transcript(stored) if keep_transcript else {}
        merged = {k: v for k, v in {**stored, **own, **kept}.items() if k not in drop}
        if merged == stored:
            return stored
        try:
            await messaging_repo.patch_room_meta(
                room_id, {"meeting": merged}, expected_revision=int(meta.get("revision", 0))
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 3:
                raise  # a throttle, access error, or a third lost race: SES retries the whole mail
            continue
        if meta.get("ownerId"):
            await agent_email.mail_changed(meta["ownerId"])
        return merged


# ``audio`` is how the bot hands over sound ("separate": one LiveKit seat per Meet person, see
# meet_bridge); it belongs to the bot, so it goes with it.
_BOT_KEYS = ("recallBotId", "ticketNonce", "botClaimAt", "botError", "audio")
_CLAIM_S = 30  # a claimed-but-unwritten booking younger than this may be another delivery's POST in flight


def _title(meeting: dict) -> str:
    return meeting.get("title") or "Meeting"


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
        f"Couldn't {verb} a bot for **{_title(meeting)}**: {reason}. Forward the invite again to retry.",
    )
    return await _patch_meeting(
        room_id, {"status": "bot_failed", "botError": code}, drop=() if keep else ("ticketNonce", "botClaimAt")
    )


def _over(start_at: float | None, end_at: float | None, now: float) -> bool:
    """An occurrence that has ended: its end has passed, or (no end) it started over an hour ago."""
    if end_at:
        return end_at <= now
    return start_at is not None and start_at < now - 3600


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
                f"Couldn't pull the bot out of **{_title(meeting)}**.",
            )
            return await _patch_meeting(room_id, {"botError": code})
        return await _patch_meeting(room_id, {}, drop=_BOT_KEYS)
    if _over(meeting.get("startAt"), meeting.get("endAt"), time.time()):
        # Never a bot into a slot that is over: Recall would send it into the Meet at once.
        logger.warning("meeting room %s: its time has passed, no bot booked or moved", room_id)
        return meeting
    if bot_id and meeting["status"] != "bot_failed" and prior and prior.get("meetingUrl") == meeting["meetingUrl"]:
        if int(prior.get("startAt", 0)) == meeting["startAt"]:
            return meeting
        try:
            await meeting_bots.reschedule(bot_id, room_id=room_id, meeting=meeting)
            # The move re-sent the recording config, so a bot booked before per-person audio has it now:
            # the record must say so, or the page would publish the mixed stream beside the bridge.
            return await _patch_meeting(room_id, {"audio": "separate"}) or meeting
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
                f"Couldn't move the bot for **{_title(meeting)}**; it will join at the old time.",
            )
            return await _patch_meeting(room_id, {"status": "bot_failed", "botError": code})
        meeting = await _patch_meeting(room_id, {"status": "scheduled"}, drop=_BOT_KEYS)
    # One booking per meeting: the nonce is claimed with a conditional write and rides in the bot's
    # metadata. Recall does not deduplicate a create, so a create under a nonce an EARLIER attempt
    # claimed is preceded by a lookup that adopts the bot a lost answer or a dead process left
    # behind. A nonce claimed just now has no bot, and List Bots is 60/min for the whole workspace.
    nonce, now = secrets.token_urlsafe(32), int(time.time())
    bot_id = None
    if not await messaging_repo.set_meeting_field_if_absent(room_id, "ticketNonce", nonce, {"botClaimAt": now}):
        stored = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if stored.get("recallBotId"):
            return stored
        nonce = stored.get("ticketNonce") or nonce
        try:
            bot_id = await meeting_bots.find_for_room(room_id, nonce)
        except Exception as exc:  # noqa: BLE001 -- a bot may stand under this nonce: keep it
            return await _bot_failed(owner_id, room_id, meeting, exc, "book", keep=True)
        if not bot_id and stored.get("status") != "bot_failed" and now - int(stored.get("botClaimAt", 0)) < _CLAIM_S:
            return stored  # a claim this young may be another delivery's POST; past it, the lease says it died
    adopted = bot_id
    org = {} if bot_id else await clerk_admin.get_organization(owner_id) or {}
    try:
        bot_id = bot_id or await meeting_bots.create(
            room_id=room_id, meeting={**meeting, "ticketNonce": nonce}, company=org.get("name") or "Your team"
        )
    except meeting_bots.RecallError as exc:
        if exc.code == "409":
            # The same key is still in flight at Recall and no bot id is stored: never acknowledge.
            # SES redelivers; the retry reuses the stored nonce and gets the cached bot.
            raise MeetingBusy(room_id) from None
        return await _bot_failed(owner_id, room_id, meeting, exc, "book", keep=not _definitive(exc))
    except Exception as exc:  # noqa: BLE001
        return await _bot_failed(owner_id, room_id, meeting, exc, "book", keep=True)
    audio = await adopted_audio(room_id, bot_id) if adopted else "separate"
    booked = {"recallBotId": bot_id, "ticketNonce": nonce, **({"audio": audio} if audio else {})}
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


async def handle_team_mail(
    owner_id: str, payload, team: dict, *, verified_sender_user_id: str | None = None
) -> str | None:
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
    # The shared-address router can resolve a verified secondary email to a Clerk user.
    # Recheck current workspace membership here; no value from the mail payload grants authority.
    members = await clerk_admin.list_organization_members(owner_id, strict=True)
    if verified_sender_user_id is not None:
        creator = next((m["user_id"] for m in members if m.get("user_id") == verified_sender_user_id), None)
    else:
        by_email = {str(m["email"]).lower(): m["user_id"] for m in members if m.get("email")}
        creator = by_email.get(sender) if sender else None
    if creator is None:
        return None  # a stranger's mail: no bot, no room -- the CEO reads it as mail
    invite = await asyncio.to_thread(parse, payload.calendar)  # the rule walk is CPU work
    if invite is None or (invite.method == "REQUEST" and not invite.meeting_url):
        return None  # not a Meet invite we can join: the CEO reads it as mail
    if invite.method == "REQUEST" and _over(
        invite.start.timestamp(), invite.end.timestamp() if invite.end else None, time.time()
    ):
        # Something already over is never a meeting: an old occurrence Google sends along with a
        # series ("All events"), or a move into the past. No record, no line, no bot, no RSVP.
        put_metric("meeting.invite.stale")
        return "meeting_ignored"
    series_room = messaging_repo.meeting_room_id(owner_id, invite.instance_of) if invite.instance_of else None
    if series_room and ((await messaging_repo.get_room(series_room)) or {}).get("meeting"):
        return await _apply_instance(owner_id, series_room, invite, creator, team, sender)
    room_id = messaging_repo.meeting_room_id(owner_id, invite.uid)
    # The lease serializes everything below (sequence check, META, line, bot, RSVP) across
    # concurrent deliveries; the loser 503s and SES redelivers after the holder is done.
    async with meeting_lease(room_id):
        return await _apply(owner_id, invite, room_id, creator, team, sender)


async def _apply_instance(
    owner_id: str, parent_room: str, invite: Invite, creator: str, team: dict, sender: str
) -> str:
    """The original slot belongs either to the series or to this deterministic child, never both.

    Always acquire parent before child. Advancing the parent and a master resend therefore
    observe the exclusion before either can book the original slot again.
    """
    room_id = messaging_repo.meeting_room_id(owner_id, invite.uid)
    async with meeting_lease(parent_room), meeting_lease(room_id):
        parent = await messaging_repo.get_room(parent_room) or {}
        meeting = parent.get("meeting") or {}
        if (
            parent.get("ownerId") != owner_id
            or meeting.get("organizer") != invite.organizer_email
            or meeting.get("uid") != invite.instance_of
            or not meeting.get("recurrence")
            or parent.get("archivedAt")
            or parent.get("deletedAt")
        ):
            return "meeting_ignored"
        child = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
        if int(child.get("sequence", -1)) > invite.sequence or (
            child.get("status") == "cancelled"
            and invite.method == "REQUEST"
            and int(child.get("sequence", -1)) >= invite.sequence
        ):
            return "meeting_stale"
        # The parser retains the original occurrence's timezone, independently of its new DTSTART.
        match = _LINE.match(invite.recurrence_id_line)
        name, value = match.groups()
        params = name.partition(";")[2]
        original = int(_when(params, value).timestamp())
        if meeting.get("status") == "cancelled" and not (
            str(original) in meeting.get("instanceRooms", {}) and int(meeting.get("startAt", 0)) == original
        ):
            return "meeting_ignored"  # a cancelled whole series cannot be revived by a late exception
        children = {**meeting.get("instanceRooms", {}), str(original): room_id}
        meeting = await _patch_meeting(parent_room, {"instanceRooms": children})
        if int(meeting.get("startAt", 0)) == original:
            # Stop/adopt the old bot through its existing cancellation transition. A failed
            # leave keeps its identity and retries; no child is booked while it might still join.
            cancelled = await _patch_meeting(parent_room, {"status": "cancelled"})
            cancelled = await _sync_bot(owner_id, parent_room, cancelled, meeting)
            if cancelled.get("recallBotId"):
                raise MeetingBusy(parent_room)
            ended = await _patch_meeting(parent_room, {"status": "ended"})
            await _reschedule_locked(owner_id, parent_room, ended, now=time.time())
        if invite.method == "CANCEL" and not child:
            # Keep the cancellation sequence even when the REQUEST is late or never delivered.
            # Normal _apply then uses the same cancellation/idempotency path as every meeting.
            await _create_room(
                owner_id,
                room_id,
                invite,
                creator,
                {
                    "uid": invite.uid,
                    "sequence": invite.sequence,
                    "status": "cancelled",
                    "organizer": invite.organizer_email,
                    "invitedBy": sender,
                    "title": invite.summary or meeting.get("title") or "Meeting",
                    "startAt": original,
                    "endAt": original + int(meeting["recurrence"]["duration"]),
                    "meetingUrl": invite.meeting_url or meeting.get("meetingUrl"),
                },
            )
        outcome = await _apply(owner_id, invite, room_id, creator, team, sender)
        if invite.method == "CANCEL":
            cancelled = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
            if cancelled.get("recallBotId"):
                raise MeetingBusy(room_id)  # keep SES retrying until the provider actually pulls it
        return outcome


def _with_overrides(recurrence: dict, meeting: dict) -> dict:
    return {
        **recurrence,
        "exdates": sorted({int(t) for t in recurrence["exdates"]} | {int(t) for t in meeting.get("instanceRooms", {})}),
    }


_ENDED = ("ended", "bot_failed")  # an occurrence is over; only advance() schedules a series' next one


async def _apply(owner_id: str, invite: Invite, room_id: str, creator: str, team: dict, sender: str) -> str | None:
    room = await messaging_repo.get_room(room_id) or {}
    if room.get("archivedAt") or room.get("deletedAt"):
        return "meeting_ignored"  # a member closed this meeting's room; a move/cancel must not reopen it
    current = room.get("meeting")
    if invite.recurrence and current and current.get("instanceRooms"):
        recurrence = _with_overrides(invite.recurrence, current)
        nxt = await asyncio.to_thread(next_occurrence, recurrence, time.time())
        if nxt is None:
            return "meeting_ignored"
        invite = replace(invite, recurrence=recurrence, start=nxt[0], end=nxt[1])
    if current and int(current.get("sequence", 0)) > invite.sequence:
        return "meeting_stale"
    allowed = settings.meetings_available()
    now = time.time()
    status = (current or {}).get("status")
    series = invite.method == "REQUEST" and bool(current) and bool(current.get("recurrence") or invite.recurrence)
    # A series' bot in (or due in) its call is never re-dated by an invite: advance() does it after.
    live = series and (status == "in_call" or (status == "bot_created" and int(current.get("startAt", 0)) <= now))
    # The invite parses to an occurrence already under way while the stored one is ahead (advance
    # booked it after an early end): the booked occurrence stands, no bot goes into the old slot.
    ahead = (
        series
        and status in ("scheduled", "bot_created")
        and invite.start.timestamp() < now < int(current.get("startAt", 0))
    )
    over = series and bool(invite.recurrence) and status in _ENDED  # re-dated by _reschedule_locked
    # A finished series made a single event: a fresh booking for it, never a move of the old bot.
    one_off = series and not invite.recurrence and status in _ENDED
    revive = one_off and int((invite.end or invite.start).timestamp()) > now
    keep_dates = live or ahead or over
    hold_bot = keep_dates or (one_off and not revive)  # no _sync_bot for the date
    if invite.method == "CANCEL":
        if not current or current.get("organizer") != invite.organizer_email:
            return "meeting_ignored"
        start_at = int(current.get("startAt", 0))  # a cancellation need not carry DTSTART
        fields = {"status": "cancelled", "sequence": invite.sequence, "invitedBy": sender}
        text = f"**{_title(current)}** was cancelled."
    else:
        start_at = int(invite.start.timestamp())
        # The invite owns these keys only; status, bot and RSVP keys belong to their own writers.
        fields = {
            "uid": invite.uid,
            "sequence": invite.sequence,
            "title": invite.summary or "Meeting",
            "startAt": start_at,
            "endAt": int(invite.end.timestamp()) if invite.end else None,
            "meetingUrl": invite.meeting_url,
            "organizer": invite.organizer_email,
            "invitedBy": sender,  # the member the after-meeting mail goes to; the ORGANIZER is sender-written
        }
        if invite.recurrence:
            fields["recurrence"] = invite.recurrence
        if keep_dates:
            del fields["startAt"], fields["endAt"]
        moved = (
            current
            and not keep_dates
            and status not in ("cancelled", *_ENDED)
            and int(current.get("startAt", 0)) != start_at
        )
        when = _when_text(invite.start)
        if moved:
            text = f"**{fields['title']}** moved to {when}."
        elif invite.recurrence:
            text = f"Agents will join **{fields['title']}** each time it meets. Next: {when}."
        else:
            text = f"Agents will join **{fields['title']}** on {when}."
        if not allowed:  # the meeting is still recorded; no bot books, no RSVP goes out
            text = f"Meetings aren't available in this environment yet, so no agents will join **{fields['title']}**."
    # The line's key is the invite's own transition: a new room counts as coming from "scheduled",
    # so a retry after the create replays its line; cancelled -> scheduled is a new key, so a
    # same-sequence re-invite announces itself again.
    before = "cancelled" if status == "cancelled" else "scheduled"
    after = "cancelled" if invite.method == "CANCEL" else "scheduled"
    if current is None:
        await _create_room(owner_id, room_id, invite, creator, {**fields, "status": "scheduled"})
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
    drop = ("recurrence",) if invite.method == "REQUEST" and not invite.recurrence else ()
    if revive:
        fields["status"], drop = "scheduled", (*drop, *_OCCURRENCE_KEYS)
    meeting = await _patch_meeting(room_id, fields, drop=drop, keep_transcript=revive)
    if meeting is None:
        return "meeting_stale"  # a newer invite landed meanwhile: it owns the line, the bot and the RSVP
    # Every delivery that leaves a record standing, so a swallowed first failure heals. The inbox
    # listing must never fail team mail.
    try:
        await agent_email_repo.add_meeting(team.get("address") or "", room_id)
    except Exception:  # noqa: BLE001
        logger.warning("meeting room %s: recording it on the team row failed", room_id, exc_info=True)
    # After the listing write, so a panel's refetch on this nudge finds a new meeting.
    await agent_email.mail_changed(owner_id)
    if over and allowed:
        # A re-forward restarts a parked series: the next occurrence, then its line.
        if await _reschedule_locked(owner_id, room_id, meeting, now=now) == "series_over":
            return "meeting_ignored"
        meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or meeting
        start_at = int(meeting["startAt"])
        text = (
            f"Agents will join **{fields['title']}** each time it meets. "
            f"Next: {_when_text(datetime.fromtimestamp(start_at, invite.start.tzinfo))}."
        )
    # META first, then the line: only a write that stood announces itself, and a failure between
    # them retries (under the lease) into the same keyed line.
    # A replayed cancel (already cancelled, sequence not higher) announces nothing; the bot sync below still retries.
    replayed_cancel = (
        invite.method == "CANCEL"
        and (current or {}).get("status") == "cancelled"
        and int(current.get("sequence", 0)) >= invite.sequence
    )
    if not replayed_cancel:
        await _post_line(owner_id, room_id, f"meeting:{invite.uid}:{invite.sequence}:{start_at}:{before}>{after}", text)
    if not hold_bot:  # the bot in a live call, or a booked next occurrence, is never moved by an invite
        meeting = await _sync_bot(owner_id, room_id, meeting, current)
    if invite.method == "CANCEL":
        # The master owns future scheduling even when an exception owns its own bot.
        # Parent-before-child is the same ordering used by an instance update.
        for child_room in meeting.get("instanceRooms", {}).values():
            async with meeting_lease(child_room):
                child = ((await messaging_repo.get_room(child_room)) or {}).get("meeting") or {}
                if int(child.get("endAt", 0)) > time.time():
                    cancel = replace(
                        invite,
                        uid=child["uid"],
                        recurrence=None,
                        sequence=max(invite.sequence, int(child.get("sequence", 0))),
                    )
                    await _apply(owner_id, cancel, child_room, creator, team, sender)
                    remaining = ((await messaging_repo.get_room(child_room)) or {}).get("meeting") or {}
                    if remaining.get("recallBotId"):
                        raise MeetingBusy(child_room)
    logger.info("meeting room %s: %s", room_id, meeting["status"])
    if invite.method == "CANCEL":
        return "meeting_cancelled"
    if (
        allowed
        # "going" only when a bot really is: a failed or unbooked meeting sends no RSVP
        and meeting.get("recallBotId")
        and meeting.get("status") in ("bot_created", "in_call")
        and not team.get("outbound_disabled")
        and meeting.get("rsvpSequence") != invite.sequence
        and await meeting_rsvp.accept(owner_id, invite)
    ):
        await _patch_meeting(room_id, {"rsvpSequence": invite.sequence})
    return "meeting_scheduled"


_CALL_KEYS = (
    "inCallAt",
    "lastBotStatus",
    "transcriptAttachedAt",
    "transcriptId",
    "transcriptRoomId",
    "notesRunId",
    "notes",
    "decisions",
    "notesMailedAt",
    "notesMailAttemptedAt",
    "roster",
    "endedAt",
    "notesSkipped",
    "unsaid",
    "limitSaid",
    "announced",
    "asked",
    "followUps",
    "summary",
    "linksSaid",
)
_OCCURRENCE_KEYS = (*_BOT_KEYS, *_CALL_KEYS)
_HISTORY_MAX = 30
_HISTORY_UNSAID = 3  # unsaid is up to ~44 KB an occurrence: only the newest few keep it (META is 400 KB)


def _kept_transcript(meeting: dict) -> dict:
    """Before the occurrence keys are dropped: its transcript, end time, notes state and unsaid
    answers stay readable from the inbox."""
    if not meeting.get("transcriptId"):
        return {}
    past = {k: meeting.get(k) for k in ("startAt", "transcriptId", "transcriptRoomId")}
    past |= {
        k: meeting[k]
        for k in ("endedAt", "notesRunId", "notesSkipped", "notes", "decisions", "unsaid")
        if meeting.get(k)
    }
    history = [past, *(meeting.get("history") or [])][:_HISTORY_MAX]
    return {
        "history": [
            h if i < _HISTORY_UNSAID else {k: v for k, v in h.items() if k != "unsaid"} for i, h in enumerate(history)
        ]
    }


async def _reschedule_locked(owner_id: str, room_id: str, meeting: dict, *, now: float) -> str:
    """A series occurrence that is over, or whose booking failed, gets its next bot. The caller
    holds ``meeting_lease``. Returns "series_over", "bot_failed" or "advanced"; a failure after
    the write is ``bot_failed`` with a line, never ``scheduled`` without a bot."""
    end = int(meeting.get("endAt") or meeting.get("startAt") or 0)
    # bot_failed with no call and no bot status, its occurrence still ahead: the BOOKING failed. A
    # plain retry of the same occurrence keeps its dates and its nonce (_BOT_KEYS), so a create
    # Recall did take is adopted. Anything else is a new occurrence and a new booking: no old nonce,
    # which would adopt (or replay from Recall's 1 h cache) the bot for a date that passed.
    booking = meeting.get("status") == "bot_failed" and not (meeting.get("inCallAt") or meeting.get("lastBotStatus"))
    if booking and end > now:
        fields, drop = {}, _CALL_KEYS
    else:
        nxt = await asyncio.to_thread(next_occurrence, _with_overrides(meeting["recurrence"], meeting), max(now, end))
        if nxt is None:
            return "series_over"
        fields = {"startAt": int(nxt[0].timestamp()), "endAt": int(nxt[1].timestamp()), "status": "scheduled"}
        drop = _OCCURRENCE_KEYS
    written = await _patch_meeting(room_id, fields, drop=drop, keep_transcript=True)
    await _seat_new_agents(owner_id, room_id)
    try:
        result = await _sync_bot(owner_id, room_id, written, meeting)
    except Exception as exc:  # noqa: BLE001 -- MeetingBusy (a Recall 409) included
        result = await _bot_failed(owner_id, room_id, written, exc, "book", keep=True)
    return "bot_failed" if result.get("status") == "bot_failed" else "advanced"


async def _seat_new_agents(owner_id: str, room_id: str) -> None:
    """Agents hired since the room was made join it on ``mentions``. Best effort."""
    try:
        present = {m["id"] for m in await messaging_repo.list_members(room_id)}
        roster = await messaging.load_agent_roster(owner_id)
        handles = messaging.mention_handles(roster)
        for a in roster:
            if a["id"] in present or len(present) >= messaging_repo.MAX_ROOM_MEMBERS:
                continue
            member = {"kind": "agent", "id": a["id"], "displayName": a["name"], "role": "member"}
            member |= {"mentionHandle": handles[a["id"]], "listenMode": "mentions"}
            await messaging_repo.upsert_member(room_id=room_id, owner_id=owner_id, member=member)
            present.add(a["id"])
    except Exception:  # noqa: BLE001 -- the call still runs with the agents already seated
        logger.warning("meeting room %s: seating new agents failed", room_id, exc_info=True)


_ADVANCE_RUNS, _ADVANCE_GAP_S = 3, 20


async def advance(room_id: str) -> None:
    """A series' next occurrence, once this one is over (``ended`` or ``bot_failed``). A booking
    that fails is retried twice, 20 s apart, so a Recall blip does not park a daily series. Never
    raises; every outcome is a metric."""
    outcome = "error"
    try:
        for run in range(_ADVANCE_RUNS):
            if run:
                await asyncio.sleep(_ADVANCE_GAP_S)
            outcome = await _advance(room_id)
            if outcome != "bot_failed":
                break
    except Exception:  # noqa: BLE001 -- a later terminal webhook or a re-forward retries
        outcome = "error"
        logger.warning("meeting room %s: advance failed", room_id, exc_info=True)
    put_metric("meeting.advance", dimensions={"outcome": outcome})


async def _advance(room_id: str) -> str:
    if not (((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}).get("recurrence"):
        return "not_series"  # the common one-off: no lease taken
    try:
        async with meeting_lease(room_id, tries=4):  # an end path may hold it for a moment
            meta = await messaging_repo.get_room(room_id) or {}
            meeting = meta.get("meeting") or {}
            if not meeting.get("recurrence") or meeting.get("status") not in _ENDED:
                return "not_due"
            return await _reschedule_locked(meta["ownerId"], room_id, meeting, now=time.time())
    except MeetingBusy:
        return "busy"
