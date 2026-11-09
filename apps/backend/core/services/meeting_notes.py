"""After a meeting: its transcript lands in the meeting's output room (the workspace's default
room) as ``transcript.txt`` (never as messages), one CEO run writes the notes there, and the
notes stay in the app; nothing is emailed automatically. A series then moves on to its next occurrence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime

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
    """One app-only notes run per occurrence, then a series' advance (after the
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
                "yourself. Action items are recorded commitments, not authorization to start work. Action items come only from what people "
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
        return  # its notes run was dispatched: its reply keeps the notes in the app
    # Automatic meeting notes are app-only. Keep the existing completion marker for old rows.
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
    "title", "invitedBy", "organizer", "transcriptId", "transcriptRoomId", "transcriptAttachedAt", "unsaid", "inCallAt", "endedAt", "endAt",
)  # fmt: skip


async def on_agent_reply(owner_id: str, agent_id: str, reply_to: str | None, text: str, progress: bool) -> None:
    """Keep the requested notes in the app. Only the assigned agent's final reply qualifies.
    Never raises: the reply is already stored."""
    if progress or not reply_to or not text.strip():
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, reply_to)
        if request is not None and request.get("agentId") == agent_id:
            await _keep_notes(owner_id, reply_to, request, notes=text)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes keep failed for reply to %s", reply_to, exc_info=True)


async def on_notes_run_ended(owner_id: str, row: dict) -> None:
    """Keep a notes run's final reply, including one that arrived before its request record.
    A failed run is marked complete without emailing anyone. Never raises."""
    trigger = row.get("triggerMessageId")
    if not trigger:
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, trigger)
        if request is None or request.get("mailAttemptedAt"):
            return
        notes = await _posted_notes(request, trigger) if row.get("outcome") == "completed" else None
        await _keep_notes(owner_id, trigger, request, notes=notes)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes end-of-run keep failed for %s", trigger, exc_info=True)


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


def same_action(item: str, task: dict) -> bool:
    """Only an exact normalized title/description after the notes' structural owner prefix.
    Shared verbs or object words are not proof that a commitment is this task."""
    normalize = lambda text: " ".join(re.findall(r"\w+", text.casefold()))  # noqa: E731
    action = normalize(item.split(":", 1)[-1])
    return bool(action) and any(action == normalize(task.get(k) or "") for k in ("title", "description"))


async def _meeting_tasks(owner_id: str, room_id: str, since: int, until: int) -> list[dict]:
    """The meeting room's tasks started during this occurrence (open or done): the tasks holding, or that
    held, the room's threads (`list_room_threads`), never a workspace-wide scan."""
    threads = await tasks_repo.list_room_threads(owner_id, room_id)
    ids = dict.fromkeys(tid for t in threads for tid in (t["taskId"], *(t.get("history") or [])))
    rows = await tasks_repo.batch_get(owner_id, [ts.task_sk(tid) for tid in ids])

    def in_call(row: dict) -> bool:
        try:
            at = datetime.fromisoformat(row.get("createdAt") or "").timestamp()
        except ValueError:
            return False
        audience = row.get("audience") or {}
        return (
            since <= at <= until
            and row.get("origin") == "meeting"
            and audience.get("kind") == "room"
            and audience.get("roomId") == room_id
        )

    return [r for r in rows if in_call(r)]


async def _file_action_items(owner_id: str, message_id: str, request: dict, notes: str) -> None:
    """Record commitments, not execution requests. Notes do not authorize work for the inviter.
    A unique matching task already started in this call is linked, including finished work; ambiguous or
    unmatched items remain recorded. Identity belongs to the occurrence and text, not the notes retry."""
    room_id = request["roomId"]
    meta = (await messaging_repo.get_room(room_id)) or {}
    if meta.get("ownerId") != owner_id or not meta.get("meeting"):
        return
    meeting = meta["meeting"]
    occurrence = request.get("occurrence", meeting.get("startAt"))
    entry = (
        meeting
        if meeting.get("startAt") == occurrence
        else next((h for h in meeting.get("history") or [] if h.get("startAt") == occurrence), {})
    )
    snapshot = request.get("meeting") or {}
    since = int(snapshot.get("inCallAt") or entry.get("inCallAt") or 0)
    until = int(snapshot.get("endedAt") or entry.get("endedAt") or snapshot.get("endAt") or entry.get("endAt") or 0)
    items = action_items(notes)
    tasks = await _meeting_tasks(owner_id, room_id, since, until) if items and since and until >= since else []
    additions = []
    statuses = {
        "queued": "queued",
        "working": "working",
        "waiting": "needs_you",
        "failed": "needs_you",
        "idle": "done",
        "resolved": "done",
    }
    for text in items:
        identity = f"meeting-action:{owner_id}:{room_id}:{occurrence}:{' '.join(text.casefold().split())}"
        item = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)), "text": text, "status": "recorded"}
        matches = {t["taskId"]: t for t in tasks if t.get("taskId") and same_action(text, t)}
        if len(matches) == 1:
            task = next(iter(matches.values()))
            item.update(taskId=task["taskId"], status=statuses.get(task.get("status"), "recorded"))
            if task.get("triggerId"):
                item["sourceTriggerId"] = task["triggerId"]
        additions.append(item)

    def merge(entry: dict) -> dict:
        kept = list(entry.get("followUps") or [])
        ids = {f.get("id") for f in kept}
        for item in additions:
            if item["id"] not in ids:
                kept.append(item)
                ids.add(item["id"])
        return {"followUps": kept}

    def lay(m: dict) -> dict:
        if m.get("startAt") == occurrence:
            return merge(m)
        return {"history": [{**h, **merge(h)} if h.get("startAt") == occurrence else h for h in m.get("history") or []]}

    if additions:
        await meeting_invites._patch_meeting(room_id, lay)


async def _keep_notes(owner_id: str, message_id: str, request: dict, notes: str | None) -> None:
    """The notes agent's reply is kept on the meeting record (the app's timeline); nothing is mailed. A recurring
    series has advanced by the time the reply lands: the notes go into that occurrence's history entry. The
    write is idempotent, so the once-only claim is spent only after it lands."""
    room_id, occurrence = request["roomId"], request.get("occurrence")
    text = (notes or "").strip()

    def lay(m: dict) -> dict:
        if m.get("startAt") == occurrence:
            return {"notesMailedAt": int(time.time()), **({"notes": text} if text and not m.get("notes") else {})}
        past = [{**h, "notes": text} if text and h.get("startAt") == occurrence and not h.get("notes") else h
                for h in m.get("history") or []]  # fmt: skip
        return {"history": past}

    saved = await meeting_invites._patch_meeting(room_id, lay)
    await messaging_repo.claim_meeting_notes_mail(owner_id, message_id)

    entry = (
        saved
        if saved and saved.get("startAt") == occurrence
        else next((h for h in (saved or {}).get("history") or [] if h.get("startAt") == occurrence), {})
    )
    if canonical := (entry or {}).get("notes"):
        try:
            await _file_action_items(owner_id, message_id, request, canonical)
        except Exception:  # noqa: BLE001 -- the notes stay in the app; the items are in the notes
            put_metric("meeting.notes.actions_failed")
            logger.warning("meeting room %s: action items not filed", room_id, exc_info=True)
