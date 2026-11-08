"""After a meeting: its transcript lands in the meeting's output room (the workspace's default
room) as ``transcript.txt`` (never as messages), one CEO run writes the notes there, and the
organizer gets one mail with the room link. A series then moves on to its next occurrence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core import tasks_schema as ts
from core.repositories import agent_email_repo, container_repo, messaging_repo, tasks_repo
from core.services import (
    agent_email,
    agent_mail_send,
    agent_mail_tenant,
    meeting_invites,
    messaging,
    openclaw_work,
    ses_email,
)
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS

logger = logging.getLogger(__name__)

_WAIT_S, _POLL_S = 60, 2  # outlasts the worker's 45 s final-transcript retry budget
_CLAIM_S = 120  # a notes attempt younger than this is still running; older, it died and is retried
_NOTES_TRIES, _NOTES_RETRY_S = 3, 10


def owed(meeting: dict) -> bool:
    """A call that happened (the bot was in it) and is over, whose notes have not finished."""
    held = meeting.get("status") in ("ended", "bot_failed") and meeting.get("inCallAt")
    # An attempted mail is done: a send that failed or timed out may have delivered, and a
    # duplicate is worse than a lost mail.
    # A dispatched notes run mails with its reply (on_agent_reply / on_notes_run_ended).
    return bool(held) and not (
        meeting.get("notesMailedAt") or meeting.get("notesMailAttemptedAt") or meeting.get("notesRunId")
    )


#: How the voice's own lines are marked in the transcript: the notes take action items only from people.
ASSISTANT_MARK = " (the assistant)"


def _render(lines: list[dict]) -> str:
    return "".join(
        f"[{time.strftime('%H:%M', time.gmtime(x.get('at') or 0))}] {x['name']}"
        f"{ASSISTANT_MARK if x.get('kind') == 'agent' else ''}: {x['text']}\n"
        for x in lines
    )


async def on_transcript(room_id: str, lines: list[dict], final: bool) -> None:
    """Attach a meeting's whole transcript (its ``final`` request) to the room as
    ``transcript.txt``, once. Rolling flushes are only logged by the route."""
    if not final or not lines:
        return
    meta = await messaging_repo.get_room(room_id) or {}
    meeting = meta.get("meeting") or {}
    if meeting.get("transcriptAttachedAt") or not meta.get("ownerId"):
        return
    if meeting.get("status") not in ("in_call", "ended", "bot_failed") or not meeting.get("inCallAt"):
        put_metric("meeting.transcript.dropped")  # no call, or late: the series moved on to its next occurrence
        return
    owner_id, occurrence = meta["ownerId"], meeting.get("startAt")
    target = await meeting_invites.output_room(owner_id, room_id)
    data = _render(sorted(lines, key=lambda x: int(x["mark"]))).encode()
    # Upload, post, then mark: a crash in between retries into a replayed post (one message), at
    # worst leaving an orphaned S3 object -- never a meeting with no transcript.
    attachment = await run_in_thread(messaging.store_attachment, owner_id, target, "transcript.txt", "text/plain", data)
    await meeting_invites._post_line(
        owner_id,
        target,
        f"meeting:{room_id}:{occurrence}:transcript",
        f"Transcript of **{meeting.get('title') or 'Meeting'}** attached.",
        attachments=[attachment.model_dump(mode="json", by_alias=True)],
    )
    await meeting_invites._patch_meeting(
        room_id,
        {"transcriptAttachedAt": int(time.time()), "transcriptId": str(attachment.id), "transcriptRoomId": target},
        when=lambda m: m.get("startAt") == occurrence,
    )


async def on_ended(room_id: str) -> None:
    """One notes run and one organizer mail per occurrence, then a series' advance (after the
    attempt, success or failure). Fire-and-forget: never raises.

    There is no sweeper: the retry is Recall's own webhooks. ``done`` follows ``call_ended`` (and a
    failed delivery is redelivered), and the status route calls this again on every terminal webhook
    while the notes are ``owed`` -- so an attempt that failed here is finished by the next one."""
    try:
        attempted = await _on_ended(room_id)
    except Exception:  # noqa: BLE001
        attempted = True
        put_metric("meeting.notes.failed")
        logger.warning("meeting room %s: notes failed", room_id, exc_info=True)
    # Not gated on the notes succeeding: a parked series is worse than one occurrence's lost notes.
    if attempted:  # not while another end path is on the notes: it advances when it is done
        await meeting_invites.advance(room_id)


async def _on_ended(room_id: str) -> bool:
    """False only when another end path holds the notes."""
    meta = await messaging_repo.get_room(room_id) or {}
    if not owed(meta.get("meeting") or {}):
        return True  # not a meeting, no call happened, or its notes are done
    # A lease, released only on a failure: a crash mid-way frees it after _CLAIM_S and the next
    # on_ended (a Recall redelivery) finishes -- each step is idempotent on its own marker.
    lease = f"meeting-notes#{room_id}"
    token = await container_repo.try_claim_owner_lifecycle(lease, stale_s=_CLAIM_S)
    if not token:
        return False  # another end path is on the notes
    # Two more tries under the same claim before the series advances past this occurrence; each
    # step of _notes is idempotent on its own marker and key. The transcript wait runs once: a
    # retry reuses what it found, so three attempts stay well inside the claim's _CLAIM_S.
    occurrence = meta["meeting"].get("startAt")  # fixed before the wait: never mark the next one
    try:
        meta = await _wait_for_transcript(room_id, meta)
    except Exception:
        await container_repo.release_owner_lifecycle_claim(lease, token=token)
        raise
    for attempt in range(_NOTES_TRIES):
        try:
            await _notes(room_id, meta, occurrence)
            return True
        except Exception:
            if attempt == _NOTES_TRIES - 1:
                # A failed attempt frees the claim at once, so the next terminal webhook retries it.
                await container_repo.release_owner_lifecycle_claim(lease, token=token)
                raise
            logger.warning("meeting room %s: notes attempt %d failed", room_id, attempt + 1, exc_info=True)
            await asyncio.sleep(_NOTES_RETRY_S)
    return True


async def _wait_for_transcript(room_id: str, meta: dict) -> dict:
    """The META to write the notes from: once its transcript is attached, or after the wait."""
    occurrence = meta["meeting"].get("startAt")
    this_occurrence = lambda m: m.get("startAt") == occurrence  # noqa: E731
    # The worker's final transcript POST and Recall's call_ended race: give the transcript a moment.
    # A read of another occurrence (the series was re-dated meanwhile) ends the wait; the last read
    # of this occurrence stands.
    for _ in range(_WAIT_S // _POLL_S):
        if (meta.get("meeting") or {}).get("transcriptAttachedAt"):
            break
        await asyncio.sleep(_POLL_S)
        fresh = await messaging_repo.get_room(room_id) or meta
        if not this_occurrence(fresh.get("meeting") or {}):
            break
        meta = fresh
    return meta


async def _notes(room_id: str, meta: dict, occurrence) -> None:
    this_occurrence = lambda m: m.get("startAt") == occurrence  # noqa: E731 -- never mark the next one
    owner_id, meeting = meta["ownerId"], meta["meeting"]
    title = meeting.get("title") or "Meeting"
    target = await meeting_invites.output_room(owner_id, room_id)
    if not meeting.get("transcriptAttachedAt"):
        # No notes run: the output room is the workspace's default room, whose history is not the
        # meeting's, so notes written from it would be invented.
        await meeting_invites._post_line(
            owner_id,
            target,
            f"meeting:{room_id}:{occurrence}:no-transcript",
            f"**{title}** ended. No transcript was captured, so there are no notes.",
        )
        await meeting_invites._patch_meeting(room_id, {"notesSkipped": "no_transcript"}, when=this_occurrence)
    elif not meeting.get("notesRunId") and not meeting.get("notesSkipped"):
        writer = await _notes_writer(owner_id, target)
        if writer is None:
            put_metric("meeting.notes.skipped", dimensions={"why": "no_agent"})
            logger.warning("meeting room %s: no agent in output room %s to write the notes", room_id, target)
            meeting = await meeting_invites._patch_meeting(
                room_id, {"notesSkipped": "no_agent"}, when=this_occurrence
            ) or {**meeting, "notesSkipped": "no_agent"}
        else:
            message = await meeting_invites._post_line(
                owner_id,
                target,
                f"meeting:{room_id}:{occurrence}:notes",
                f"Write the meeting notes for {title}: decisions, open questions, two paragraphs max, then a last "
                'section headed "Action items:" with one line per item ("- <owner>: <what>"); do not create tasks '
                "yourself, each action item becomes a thread for the team. Action items come only from what people "
                f"said: never from a line marked{ASSISTANT_MARK}, which is the voice repeating the team. The "
                "transcript is attached" + _unsaid_text(meeting),
                dispatch_targets=[{"agentId": writer, "eventKind": "user_request"}],
            )
            if message:
                run_id = messaging_repo.run_id_for(writer, message["messageId"])
                # The mail waits for the notes themselves (on_agent_reply) or the run's end
                # (on_notes_run_ended). What it reads travels with the request: a recurring series
                # rewrites the meeting record as it advances, before the notes come back.
                snapshot = {k: meeting[k] for k in _MAIL_FIELDS if meeting.get(k) is not None}
                await messaging_repo.put_meeting_notes_request(owner_id, message["messageId"], {
                    "roomId": room_id, "occurrence": occurrence, "targetRoomId": target, "agentId": writer,
                    "triggerSeq": message.get("messageSeq"), "meeting": snapshot,
                })  # fmt: skip
                await meeting_invites._patch_meeting(room_id, {"notesRunId": run_id}, when=this_occurrence)
                return
    if meeting.get("notesRunId"):
        return  # its notes run was dispatched: the mail goes with its reply
    mailed = await _mail_organizer(owner_id, room_id, target, meeting, title, occurrence)
    if mailed is None:
        return  # already attempted (here or by another end path), or the occurrence moved on
    if not mailed:
        raise RuntimeError("the organizer mail was not sent")
    await meeting_invites._patch_meeting(room_id, {"notesMailedAt": int(time.time())}, when=this_occurrence)


async def _notes_writer(owner_id: str, room_id: str) -> str | None:
    """The agent that writes the notes. The run is dispatched in ``room_id``, so only its agent
    members qualify: the CEO when it sits there, else the one openclaw_work's fallback picks (a
    founder roster has no starter ids). None when the room has no agent."""
    agents = sorted(m["id"] for m in await messaging_repo.list_members(room_id) if m.get("kind") == "agent")
    return await openclaw_work._fallback_single(owner_id, agents, {}, []) if agents else None


def _unsaid_text(meeting: dict) -> str:
    """The answers agents had ready when the call moved on (voice_calls._keep_unsaid), or ""."""
    unsaid = meeting.get("unsaid") or []
    if not unsaid:
        return ""
    items = "\n\n".join(f"{u['agentName']}: {u['request'] or '(no request)'}\n{u['answer']}" for u in unsaid)
    return f"\n\nAnswers the team had ready and did not get to say:\n\n{items}"


_MAIL_TRANSCRIPT_CHARS = 200_000
_NO_NOTES = "No notes were written: no agent in the room could write them."


async def _transcript_text(owner_id: str, room_id: str, meeting: dict) -> str | None:
    if not meeting.get("transcriptId"):
        return None
    room_id = meeting.get("transcriptRoomId") or room_id  # the room it was stored for
    _, data = await run_in_thread(messaging.load_attachment, owner_id, room_id, meeting["transcriptId"])
    text = data.decode("utf-8", "replace")
    return text if len(text) <= _MAIL_TRANSCRIPT_CHARS else text[:_MAIL_TRANSCRIPT_CHARS] + "… (truncated)"


async def _mail_organizer(
    owner_id: str,
    room_id: str,
    target: str,
    meeting: dict,
    title: str,
    occurrence,
    notes: str | None = None,
    notes_failed: bool = False,
    claim=None,
) -> bool | None:
    """The one mail after a meeting. ``notes``: the notes agent's own reply (its decisions, open
    questions and the tasks it filed), which IS the mail, with the room linked for the transcript;
    ``notes_failed``: its run ended without them. Neither: a meeting with no notes run (no
    transcript, no agent). True when the notes are done: mailed, or deliberately not (no address, no organizer, a paused
    row, the owner's daily mail cap spent). False is a failed or ambiguous send and None a send
    that is not ours to make; neither is ever retried, because ``notesMailAttemptedAt`` is written
    before the send (at most once per occurrence: a lost mail beats a duplicate). With a default room the mail links to it; without one
    (``target`` is the hidden meeting room itself) nobody can open the room, so the transcript
    travels in the mail. ``claim``: a notes run's own once-only marker (its request row), used in
    place of the meeting's, which a recurring series has rewritten by the time the notes come back."""
    # The member whose invite made or last updated the meeting: the ICS ORGANIZER is sender-written
    # and may be an outside host. Records from before ``invitedBy`` fall back to it.
    organizer = meeting.get("invitedBy") or meeting.get("organizer")
    if claim is not None:  # first: every outcome below, a skip included, is the one answer
        if not await claim():
            return None
    elif meeting.get("notesMailAttemptedAt"):
        return None
    address = await agent_email.team_address(owner_id)
    if not address or not organizer:
        return True
    if (await agent_email_repo.get_by_address(address) or {}).get("outbound_disabled"):
        return True
    if not await agent_mail_send.consume_cap(owner_id):  # the same per-owner daily cap as all agent mail
        logger.info("meeting room %s: daily mail cap spent, notes mail not sent", room_id)
        return True
    name = " ".join(title.split())
    link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?room={target}"
    no_notes = meeting.get("notesSkipped") == "no_agent"
    if notes or notes_failed:
        subject = f"Notes: {name}" if notes else f"Transcript: {name}"
        body = notes.strip() if notes else f"{name} ended. The notes could not be written this time."
        if target != room_id:
            body += f"\n\nThe transcript and everything said on the call: {link}"
        else:  # nothing the organizer can open: the transcript travels in the mail
            transcript = await _transcript_text(owner_id, target, meeting)
            body += "\n\nTranscript:\n\n" + (transcript or "No transcript was captured.")
    elif target != room_id and no_notes:
        subject = f"Transcript: {name}"
        body = f"The transcript is in your Lightbulb room: {link}\n\n{_NO_NOTES}"
    elif target != room_id and meeting.get("transcriptAttachedAt"):
        subject = f"Notes: {name}"
        body = f"The notes and transcript are in your Lightbulb room: {link}"
    elif target != room_id:  # nothing to point at: no transcript, so no notes
        subject = f"Ended: {name}"
        body = f"{name} ended. No transcript was captured, so there are no notes."
    else:
        transcript = await _transcript_text(owner_id, target, meeting)
        subject = f"Transcript: {name}" if transcript else f"Ended: {name}"
        body = f"Your Lightbulb agents joined {name}.\n\n" + (transcript or "No transcript was captured.")
        if no_notes:
            body += f"\n\n{_NO_NOTES}"
    if not notes:  # the notes run was handed these answers and works them into the notes itself
        body += _unsaid_text(meeting)
    # Durable and before the send: a raise or a timeout may still have delivered it. The guard
    # refuses a moved occurrence and a marker another end path wrote first, so only one sends.
    if claim is None:
        marked = await meeting_invites._patch_meeting(
            room_id,
            {"notesMailAttemptedAt": int(time.time())},
            when=lambda m: m.get("startAt") == occurrence and not m.get("notesMailAttemptedAt"),
        )
        if marked is None:
            return None
    sent = await ses_email.send_ses_email(
        from_addr=await agent_email.public_for(address),
        to=organizer,
        subject=subject,
        body_text=body,
        headers=dict(AGENT_AUTHORED_HEADERS),
        kind="meeting_notes",
        tenant_name=await agent_mail_tenant.ensure_tenant(owner_id),
    )
    return sent is not None


#: What a notes run's mail reads from the meeting, snapshotted when the run is dispatched.
_MAIL_FIELDS = (
    "title", "invitedBy", "organizer", "transcriptId", "transcriptRoomId", "transcriptAttachedAt", "unsaid", "inCallAt",
)  # fmt: skip


async def on_agent_reply(owner_id: str, agent_id: str, reply_to: str | None, text: str, progress: bool) -> None:
    """An agent's reply to a meeting's "write the notes" request: those are the notes, and the
    organizer's mail carries them (10-09: the only mail was the raw transcript, sent before the notes
    existed). Only the agent the request was dispatched to writes them; a progress line is not the
    reply. Never raises: the reply is already stored."""
    if progress or not reply_to or not text.strip():
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, reply_to)
        if request is not None and request.get("agentId") == agent_id:
            await _mail_notes(owner_id, reply_to, request, notes=text)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes mail failed for reply to %s", reply_to, exc_info=True)


async def on_notes_run_ended(owner_id: str, row: dict) -> None:
    """Every path that closes a run calls this. A notes run whose mail has not gone sends it now:
    the notes it posted (a reply can land before the request is recorded), or, with none, one mail
    saying the notes could not be written. Never raises: the run is already closed."""
    trigger = row.get("triggerMessageId")
    if not trigger:
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, trigger)
        if request is None or request.get("mailAttemptedAt"):
            return
        notes = await _posted_notes(request, trigger) if row.get("outcome") == "completed" else None
        await _mail_notes(owner_id, trigger, request, notes=notes)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes end-of-run mail failed for %s", trigger, exc_info=True)


async def _posted_notes(request: dict, trigger: str) -> str | None:
    """The notes agent's final reply to the request, read from the room just after it."""
    seq = request.get("triggerSeq")
    if seq is None:
        return None
    rows, _ = await messaging_repo.list_messages(request["targetRoomId"], int(seq), 100)
    replies = [
        r.get("text") or "" for r in rows
        if (r.get("author") or {}).get("id") == request.get("agentId")
        and r.get("replyToMessageId") == trigger and r.get("progress") is not True
    ]  # fmt: skip
    return next((t for t in reversed(replies) if t.strip()), None)


_ACTIONS_HEAD = re.compile(r"^\W*action items\W*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.+)$")


def action_items(notes: str) -> list[str]:
    """The bullets under the notes' "Action items:" heading (the notes request asks for that section)."""
    items: list[str] = []
    lines = iter(notes.splitlines())
    for line in lines:
        if _ACTIONS_HEAD.match(line.strip()):
            for item in lines:
                if not (m := _BULLET.match(item)):
                    if items or item.strip():
                        break
                    continue
                items.append(" ".join(m.group(1).replace("**", "").split()))
            break
    return [i for i in items if i]


#: Not part of an action item's object ("Order **more** gel colors **from** OPI"). "post" and "email" are objects.
_STOP = frozenset(
    "the a an and or for with from to of on in at by more some our their his her my your it this that".split()
)
#: Verbs that name the same work ("Finalize and share the post" is the drafted post's thread).
_VERBS = (
    frozenset("draft write prepare create make finalize finish complete".split()),
    frozenset("send share email mail publish".split()),
    frozenset("order buy purchase".split()),
    frozenset("book schedule reserve".split()),
)


def _stem(word: str) -> str:
    word = word.lower().replace("colour", "color")
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _verb_object(text: str) -> tuple[set[str], set[str]]:
    """(the verbs, the object's words) of an imperative line: its first word, a second after "and" ("Finalize and
    share ..."), then the rest without `_STOP` words."""
    words = [_stem(w) for w in re.findall(r"[A-Za-z]+", text)]
    verbs = set(words[:1])
    rest = words[1:]
    if len(rest) >= 2 and rest[0] == "and":
        verbs.add(rest[1])
        rest = rest[2:]
    return verbs, {w for w in rest if w not in _STOP}


def _same_verb(a: set[str], b: set[str]) -> bool:
    return bool(a & b) or any(a & g and b & g for g in _VERBS)


def covered(item: str, titles: list[str]) -> bool:
    """A task already started in the meeting covers the item: the same verb (or one naming the same work) and every
    word of the item's object in the task's title ("Finalize and share holiday-hours post" is "Draft the holiday-hours
    post", 8 Nov prod Meet re-run LB-24 vs LB-19; "Book the venue" is not "book the venue for the party")."""
    verbs, obj = _verb_object(item.split(":", 1)[-1])
    for title in titles:
        t_verbs, t_obj = _verb_object(title)
        if obj and _same_verb(verbs, t_verbs) and obj <= t_obj:
            return True
    return False


async def _meeting_task_titles(owner_id: str, room_id: str, since: int) -> list[str]:
    """Titles of the meeting room's tasks started since the call began (open or done): the tasks holding, or that
    held, the room's threads (`list_room_threads`), never a workspace-wide scan."""
    after = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
    threads = await tasks_repo.list_room_threads(owner_id, room_id)
    ids = dict.fromkeys(tid for t in threads for tid in (t["taskId"], *(t.get("history") or [])))
    rows = await tasks_repo.batch_get(owner_id, [ts.task_sk(tid) for tid in ids])
    return [r.get("title") or "" for r in rows if (r.get("createdAt") or "") >= after]


async def _file_action_items(owner_id: str, message_id: str, request: dict, notes: str) -> None:
    """Action items -> threads (super-agent plan: "meeting action items → Requests, not Paperclip"): one message in
    the meeting's own room as the inviting member, which its Router starts a thread for each, like an addressed line
    in the call (voice_meet.ask). Keyed on the notes request, so the reply and the run's end post it once."""
    from routers.voice_meet import inviting_member

    items = action_items(notes)
    room_id = request["roomId"]
    meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting") or {}
    since = int((request.get("meeting") or {}).get("inCallAt") or meeting.get("inCallAt") or 0)
    if items and since:  # work the meeting already started, open or done, is not filed again (LB-24 vs LB-19)
        titles = await _meeting_task_titles(owner_id, room_id, since)
        # Covered items stay out of the Router's message entirely (they are in the mailed notes): listed there, the
        # Router handed them to the finished task, which ran again (8 Nov prod Meet re-run #3, LB-28 attempt 3).
        items = [i for i in items if not covered(i, titles)]
    member = inviting_member(meeting, await messaging_repo.list_members(room_id)) if items else None
    if member is None:
        return
    title = " ".join(((request.get("meeting") or {}).get("title") or meeting.get("title") or "Meeting").split())
    text = f"Action items from the meeting {title}, one thread each:\n" + "\n".join(f"- {i}" for i in items)
    await messaging.create_human_message(
        AuthContext(user_id=member, org_id=owner_id),
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"meeting-actions:{owner_id}:{message_id}"),
            text=text[:3800],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="meeting_notes",  # nobody spoke it: no spoken-reply hint (admission still tags origin=meeting)
    )


async def _mail_notes(owner_id: str, message_id: str, request: dict, notes: str | None) -> None:
    room_id, occurrence = request["roomId"], request.get("occurrence")
    meeting = request.get("meeting") or {}
    target = request.get("targetRoomId") or await meeting_invites.output_room(owner_id, room_id)

    async def claim() -> bool:
        return await messaging_repo.claim_meeting_notes_mail(owner_id, message_id)

    mailed = await _mail_organizer(
        owner_id, room_id, target, meeting, meeting.get("title") or "Meeting", occurrence,
        notes=notes, notes_failed=notes is None, claim=claim,
    )  # fmt: skip
    if mailed:  # the live record too, while it is still this occurrence
        await meeting_invites._patch_meeting(
            room_id, {"notesMailedAt": int(time.time())}, when=lambda m: m.get("startAt") == occurrence
        )
    if notes:
        try:
            await _file_action_items(owner_id, message_id, request, notes)
        except Exception:  # noqa: BLE001 -- the notes and their mail stand; the items are in the notes
            put_metric("meeting.notes.actions_failed")
            logger.warning("meeting room %s: action items not filed", room_id, exc_info=True)
