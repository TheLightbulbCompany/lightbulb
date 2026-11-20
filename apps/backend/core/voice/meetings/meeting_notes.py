"""After a meeting: its transcript lands in the meeting's output room (the workspace's default
room) as ``transcript.txt`` (never as messages), one Router task writes the notes there (a scripted
start, its Worker reading the transcript by reference), and the notes stay in the app; nothing is
emailed automatically. A series then moves on to its next occurrence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime

from core.dynamodb import run_in_thread, utc_now_iso
from core.observability.metrics import put_metric
from core import tasks_schema as ts
from core.repositories import container_repo, messaging_repo, tasks_repo
from core.services import admission, messaging, task_conversation
from core.voice.meetings import meeting_invites

logger = logging.getLogger(__name__)

_WAIT_S, _POLL_S = 60, 2  # outlasts the worker's 45 s final-transcript retry budget
_CLAIM_S = 120  # a notes attempt younger than this is still running; older, it died and is retried
_NOTES_TRIES, _NOTES_RETRY_S = 3, 10
#: A notes task's trigger id, which is also its notes request's id: `meeting-notes:<meeting room>:<occurrence>`.
NOTES_PREFIX = "meeting-notes:"
#: The transcript's share of the Worker's input. The bridge refuses an input over 200 KB (bridge/runs.py
#: MAX_INPUT_BYTES); the brief, the time and the connected apps share the rest.
TRANSCRIPT_MAX_BYTES = 150 * 1024
#: What the owner reads when a notes task cannot get its transcript (a Retry after the request expired).
TRANSCRIPT_GONE = "The transcript for this meeting is no longer available, so notes can't be written."
#: ... and when the transcript is there but a read failed for now.
TRANSCRIPT_UNREAD = "The transcript for this meeting couldn't be read just now. Try again."


class TranscriptUnavailable(RuntimeError):
    """A notes task with no transcript to read: it fails before any Worker run (activities.worker_turn). Its message
    is what the owner reads."""

    def __init__(self, message: str = TRANSCRIPT_GONE) -> None:
        super().__init__(message)


#: The first line of notes written from a cut transcript.
CUT_LINE = "These notes cover only the last part of a long meeting: the start of its transcript was too long to read."


def owed(meeting: dict) -> bool:
    """A call that happened (the bot was in it) and is over, whose notes have not finished."""
    held = meeting.get("status") in ("ended", "bot_failed") and meeting.get("inCallAt")
    # Legacy attempted/completed markers still count as done. A started notes task keeps its
    # result in the app (on_task_moved).
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
            from routers.voice_meet import inviting_member

            principal = (
                inviting_member(meeting, await messaging_repo.list_members(room_id))
                if meeting.get("invitedBy")
                else None
            )
            trigger_id = f"{NOTES_PREFIX}{room_id}:{occurrence}"
            task_id = ts.router_task_id(trigger_id, 0, 0)  # a scripted start's task id (activities._apply)
            # What the keep reads travels with the request: a recurring series rewrites the meeting
            # record as it advances, before the notes come back. Written first: the task's end reads it.
            snapshot = {k: meeting[k] for k in _MAIL_FIELDS if meeting.get(k) is not None}
            await messaging_repo.put_meeting_notes_request(owner_id, trigger_id, {
                "roomId": room_id, "occurrence": occurrence, "targetRoomId": target, "agentId": writer,
                "taskId": task_id, "meeting": snapshot, **({"principalUserId": principal} if principal else {}),
            })  # fmt: skip
            await admission.enqueue_trigger(
                owner_id,
                _notes_trigger(owner_id, trigger_id, task_id, title, writer, principal, meeting),
                room_id=target,
            )
            await meeting_invites._patch_meeting(room_id, {"notesRunId": task_id}, when=this_occurrence)
            return
    if meeting.get("notesRunId"):
        return  # its notes task was started: its end keeps the notes in the app
    # Automatic meeting notes are app-only. Keep the existing completion marker for old rows.
    await meeting_invites._patch_meeting(room_id, {"notesMailedAt": int(time.time())}, when=this_occurrence)


def _notes_brief(title: str, meeting: dict) -> str:
    """The notes task's brief: the notes request the room turn used to get, its sections unchanged."""
    return (
        f"Write the meeting notes for {title}: open questions, two paragraphs max, then a section headed "
        '"Decisions:" with one line per decision the people made ("- <what was decided> (decided by <who>)", '
        "leaving out the parenthesis when nobody said who; no section when nothing was decided), then a last "
        'section headed "Action items:" with one line per item ("- <owner>: <what>"); do not create tasks '
        "yourself. Action items are recorded commitments, not authorization to start work. Action items come only from what people "
        f"said: never from a line marked{ASSISTANT_MARK}, which is the voice repeating the team. The "
        "transcript is below" + _unsaid_text(meeting)
    )


def _notes_trigger(
    owner_id: str, trigger_id: str, task_id: str, title: str, writer: str, principal: str | None, meeting: dict
) -> ts.Trigger:
    """A scripted StartTask (``ref.start``, applied without a Router model turn, as voice-start does). The
    transcript goes by reference (``meetingNotes.requestId`` names the request, which names the attachment):
    a trigger row never carries it."""
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=f"Meeting notes: {title}"[:200],
        brief=_notes_brief(title, meeting),
        specialist=writer,
        ack=f"Writing the notes for {title}.",
        visibility="room",
    )
    now = utc_now_iso()
    return ts.Trigger(
        ws=owner_id,
        trigger_id=trigger_id,
        source="webhook",
        admission="respond",
        lane="shared",
        actor_user_id=principal,
        received_at=now,
        created_at=now,
        updated_at=now,
        # confirmAll: the transcript is anyone's speech, so every outside action the Worker tries gets a card.
        ref={
            "taskId": task_id,
            "meetingNotes": {"requestId": trigger_id},
            "start": start.model_dump(by_alias=True),
            "confirmAll": True,
        },
    )


async def _notes_writer(owner_id: str, room_id: str) -> str | None:
    """The agent the notes task runs as: the output room's front agent (Lightbulb). None when it has none."""
    try:
        return await task_conversation.front_agent_id(owner_id, room_id=room_id)
    except task_conversation.NoFrontAgent:
        return None


def _transcript_room(request: dict) -> str:
    return (request.get("meeting") or {}).get("transcriptRoomId") or request["roomId"]


async def worker_transcript(owner_id: str, request_id: str) -> str | None:
    """The notes Worker's ``## Transcript`` (activities._request), read from the attachment the request names.
    Over ``TRANSCRIPT_MAX_BYTES`` the start is cut at a line and the end kept whole (decisions and action items
    come late), and the Worker is told. None for any other task. Raises ``TranscriptUnavailable`` when the request
    has expired (2 days) or the attachment cannot be read: notes written without it would be invented."""
    if not request_id.startswith(NOTES_PREFIX):
        return None
    request = await messaging_repo.get_meeting_notes_request(owner_id, request_id)
    attachment = ((request or {}).get("meeting") or {}).get("transcriptId")
    if not attachment:
        raise TranscriptUnavailable()
    try:
        _, raw = await run_in_thread(messaging.load_attachment, owner_id, _transcript_room(request), attachment)
    except messaging.ApiProblem as exc:
        gone = exc.error.code == "MESSAGE_NOT_FOUND"
        raise TranscriptUnavailable(TRANSCRIPT_GONE if gone else TRANSCRIPT_UNREAD) from exc
    except Exception as exc:  # an S3 or network error: the transcript is there, the read failed for now
        raise TranscriptUnavailable(TRANSCRIPT_UNREAD) from exc
    if len(raw) <= TRANSCRIPT_MAX_BYTES:
        return f"## Transcript\n{raw.decode('utf-8', 'replace')}"
    # ponytail: a meeting over ~150 KB of talk (about 3 hours) loses its start; a paged reader tool if that bites.
    tail = raw[-TRANSCRIPT_MAX_BYTES:].decode("utf-8", "ignore")
    tail = tail.split("\n", 1)[1] if "\n" in tail else tail
    logger.warning(
        "meeting room %s: transcript cut from the start for the notes (%d of %d bytes kept)",
        request["roomId"], len(tail.encode()), len(raw),
    )  # fmt: skip
    return f"## Transcript\n(This meeting was long: its start was cut, only its last part is below.)\n{tail}"


async def _was_cut(owner_id: str, request: dict) -> bool:
    attachment = (request.get("meeting") or {}).get("transcriptId")
    if not attachment:
        return False
    try:
        meta, _ = await run_in_thread(
            messaging.load_attachment_head, owner_id, _transcript_room(request), attachment, 1
        )
    except (messaging.ApiProblem, ValueError):  # the notes are kept either way; only their first line is lost
        logger.warning("meeting room %s: transcript size unreadable", request["roomId"], exc_info=True)
        return False
    return meta.size > TRANSCRIPT_MAX_BYTES


def _unsaid_text(meeting: dict) -> str:
    """The answers agents had ready when the call moved on (voice_calls._keep_unsaid), or ""."""
    unsaid = meeting.get("unsaid") or []
    if not unsaid:
        return ""
    items = "\n\n".join(f"{u['agentName']}: {u['request'] or '(no request)'}\n{u['answer']}" for u in unsaid)
    return f"\n\nAnswers the team had ready and did not get to say:\n\n{items}"


#: Immutable source for a notes run and the Router's completion event; the legacy constant name is internal.
_MAIL_FIELDS = (
    "title", "invitedBy", "organizer", "transcriptId", "transcriptRoomId", "transcriptAttachedAt", "unsaid", "inCallAt", "endedAt", "endAt",
)  # fmt: skip


#: A notes task's end. `idle` is done (its output is the notes); the rest end it without notes: failed, or a
#: person's Stop, Close or Dismiss (`resolved`).
_ENDS = frozenset({"idle", "failed", "resolved"})


async def on_task_moved(task: dict, to: str) -> None:
    """Keep a notes task's result in the app when it ends (activities._announce, every status change, before its
    person-reason check). Done: the last completed attempt's output is the notes, kept with ``_KEEP_BACKOFF_S``
    retries; when every try fails, an error is logged and the meeting marked "no notes" (the notes still stand in
    the room as the task's result). Failed, stopped, closed or dismissed: the meeting is marked "no notes". Never
    raises."""
    request_id = str(task.get("triggerId") or "")
    owner_id = str(task.get("pk") or "").removeprefix("WS#")
    if not request_id.startswith(NOTES_PREFIX) or to not in _ENDS or not owner_id:
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, request_id)
        if request is None or request.get("taskId") != task.get("taskId") or request.get("mailAttemptedAt"):
            return  # not this task's, or its notes are kept (the claim is spent only then)
        notes = None
        if to == "idle":
            tree = await tasks_repo.get_task_tree(owner_id, task["taskId"])
            done = [
                a for a in tree.get("attempts") or []
                if a.get("outcome") == "completed" and (a.get("output") or "").strip()
            ]  # fmt: skip
            notes = max(done, key=lambda a: a["n"])["output"] if done else None
        if not notes:
            await _mark_no_notes(request)
            return
        if await _was_cut(owner_id, request):
            notes = f"{CUT_LINE}\n\n{notes}"
        try:
            # One bound over every try: end_attempt and transition get 30 s each, and a keep past that would run
            # beside the activity's own retry (which skips the push and usage metering).
            async with asyncio.timeout(KEEP_TOTAL_S):
                kept = await _keep_with_retries(owner_id, request_id, request, notes)
        except TimeoutError:  # our own deadline only: an outer cancel stays a CancelledError
            kept = False
        if kept:
            return
        put_metric("meeting.notes.keep_failed")
        logger.error("meeting room %s task %s: notes not kept after retries", request["roomId"], task["taskId"])
        await _mark_no_notes(request)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes keep failed for %s", request_id, exc_info=True)


#: The pauses between a notes keep's tries (three tries in all).
_KEEP_BACKOFF_S = (0.5, 1.0)
#: The whole keep, every try included, inside the calling activity's 30 s start-to-close (workflows._SHORT).
KEEP_TOTAL_S = 20


async def _keep_with_retries(owner_id: str, request_id: str, request: dict, notes: str) -> bool:
    """``_keep_notes`` until it completes (True) or the tries run out (False). Each of its steps is safe to repeat."""
    for n in range(len(_KEEP_BACKOFF_S) + 1):
        try:
            if await _keep_notes(owner_id, request_id, request, notes=notes):
                return True
        except Exception:  # noqa: BLE001 -- retried; the caller decides what a last failure means
            logger.warning("meeting notes keep try %d failed for %s", n + 1, request_id, exc_info=True)
        if n < len(_KEEP_BACKOFF_S):
            await asyncio.sleep(_KEEP_BACKOFF_S[n])
    return False


async def _mark_no_notes(request: dict) -> None:
    """The occurrence's notes are over with none kept ("skipped" in the meeting view). The request's claim stays
    unspent, so a Retry that succeeds still keeps the notes and admits their follow-ups."""
    occurrence = request.get("occurrence")
    await meeting_invites._patch_meeting(
        request["roomId"], {"notesMailedAt": int(time.time())}, when=lambda m: m.get("startAt") == occurrence
    )


async def on_not_started(owner_id: str, trigger: dict) -> None:
    """The Router took a notes trigger and started no task (the credit gate, or a batch that failed past its
    retries: activities._apply / apply_fallback): the meeting ends with no notes, as a failed task does, instead of
    reading "pending" for ever. Never raises."""
    request_id = str(trigger.get("triggerId") or "")
    if not request_id.startswith(NOTES_PREFIX):
        return
    try:
        request = await messaging_repo.get_meeting_notes_request(owner_id, request_id)
        if request is not None and not request.get("mailAttemptedAt"):
            await _mark_no_notes(request)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.mail_failed")
        logger.warning("meeting notes not-started mark failed for %s", request_id, exc_info=True)


_ACTIONS_HEAD = re.compile(r"^\W*action items\W*$", re.I)
_DECISIONS_HEAD = re.compile(r"^\W*decisions\W*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.+)$")
_DECIDED_BY = re.compile(r"^(.+?)\s*\(decided by ([^()]+)\)\W*$", re.I)


def action_items(notes: str) -> list[str]:
    """The bullets under the notes' "Action items:" heading (the notes request asks for that section)."""
    return _section(notes, _ACTIONS_HEAD)


def decisions(notes: str) -> list[dict]:
    """The notes' own "Decisions:" section as ``[{"text", "by"?}]``: the model's lines read back, never invented."""
    out = []
    for item in _section(notes, _DECISIONS_HEAD):
        m = _DECIDED_BY.match(item)
        out.append({"text": m.group(1).strip(), "by": m.group(2).strip()} if m else {"text": item})
    return out


def _section(notes: str, head: re.Pattern) -> list[str]:
    """The bullets under the first line matching ``head``."""
    items: list[str] = []
    lines = iter(notes.splitlines())
    for line in lines:
        if head.match(line.strip()):
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


async def _keep_notes(owner_id: str, message_id: str, request: dict, notes: str | None) -> bool:
    """The notes task's result is kept on the meeting record (the app's timeline); nothing is mailed. A recurring
    series has advanced by the time the reply lands: the notes go into that occurrence's history entry. The
    write is idempotent, so the once-only claim is spent only after it lands. Without notes it only marks the
    meeting done ("no notes")."""
    room_id, occurrence = request["roomId"], request.get("occurrence")
    text = (notes or "").strip()

    # The decisions travel with the first notes kept: a later reply rewrites neither.
    first = {"notes": text, "decisions": decisions(text)} if text else {}

    def lay(m: dict) -> dict:
        if m.get("startAt") == occurrence:
            return {"notesMailedAt": int(time.time()), **(first if not m.get("notes") else {})}
        past = [{**h, **first} if first and h.get("startAt") == occurrence and not h.get("notes") else h
                for h in m.get("history") or []]  # fmt: skip
        return {"history": past}

    saved = await meeting_invites._patch_meeting(room_id, lay)
    entry = (
        saved
        if saved and saved.get("startAt") == occurrence
        else next((h for h in (saved or {}).get("history") or [] if h.get("startAt") == occurrence), {})
    )
    if canonical := (entry or {}).get("notes"):
        from core.voice.meetings import meeting_followups

        # Durable admission precedes the completion claim: retry uses the same immutable trigger.
        await meeting_followups.enqueue(owner_id, message_id, request)
        try:
            await _file_action_items(owner_id, message_id, request, canonical)
        except Exception:  # noqa: BLE001 -- the notes stay in the app; the items are in the notes
            put_metric("meeting.notes.actions_failed")
            logger.warning("meeting room %s: action items not filed", room_id, exc_info=True)
        if not await _remember_decisions(owner_id, request, entry):
            return False  # incomplete: the claim stays unspent and the caller retries (each step is repeat-safe)
        # Spent only with notes kept: a failed or stopped task leaves it unspent (the meeting's notesMailedAt says
        # "no notes"), so a Retry that succeeds still admits the follow-ups (meeting_followups.enqueue reads it).
        await messaging_repo.claim_meeting_notes_mail(owner_id, message_id)
    return True


async def _remember_decisions(owner_id: str, request: dict, entry: dict) -> bool:
    """Each decision becomes one team ``decision`` in the workspace's memory, so "what did we decide about X" is
    answered by the ordinary recall path. ``decisionsRemembered`` holds the indexes written, so a retry writes only
    the rest. False when a write failed (left for the retry); a secret-shaped refusal is final, not a failure."""
    found = entry.get("decisions") or []
    done = set(entry.get("decisionsRemembered") or [])
    from core.services import memory_api

    room_id, occurrence = request["roomId"], request.get("occurrence")
    title = (request.get("meeting") or {}).get("title") or "Meeting"
    day = time.strftime("%Y-%m-%d", time.gmtime(int(occurrence or 0)))
    ok = True
    # ponytail: a crash between one write and its mark repeats that one write; memory-side keys if that bites.
    for n, d in enumerate(found):
        if n in done:
            continue
        by = f" (decided by {d['by']})" if d.get("by") else ""
        try:
            await memory_api.remember(
                owner_id, subject=f"Decision: {title}", fact=f"Decided in the meeting {title} on {day}: {d['text']}{by}",
                kind="decision", scope="team",
            )  # fmt: skip
        except memory_api.Refused:
            put_metric("meeting.notes.decision_refused")  # secret-shaped: kept on the meeting only
        except Exception:  # noqa: BLE001 -- a transient RPC failure: left for the retry
            ok = False
            put_metric("meeting.notes.decisions_failed")
            logger.warning("meeting room %s: decision %d not remembered", room_id, n, exc_info=True)
            continue
        done.add(n)
        mark = {"decisionsRemembered": sorted(done)}

        def lay(m: dict, mark: dict = mark) -> dict:
            if m.get("startAt") == occurrence:
                return mark
            return {"history": [{**h, **mark} if h.get("startAt") == occurrence else h for h in m.get("history") or []]}

        await meeting_invites._patch_meeting(room_id, lay)
    return ok
