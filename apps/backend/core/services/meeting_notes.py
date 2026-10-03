"""After a meeting: its transcript lands in the meeting's output room (the workspace's default
room) as ``transcript.txt`` (never as messages), one CEO run writes the notes there, and the
organizer gets one mail with the room link. A series then moves on to its next occurrence.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, container_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import agent_email, agent_mail_send, agent_mail_tenant, meeting_invites, messaging, ses_email
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS

logger = logging.getLogger(__name__)

_WAIT_S, _POLL_S = 60, 2  # outlasts the worker's 45 s final-transcript retry budget
_CLAIM_S = 120  # a notes attempt younger than this is still running; older, it died and is retried


def owed(meeting: dict) -> bool:
    """A call that happened (the bot was in it) and is over, whose notes have not finished."""
    held = meeting.get("status") in ("ended", "bot_failed") and meeting.get("inCallAt")
    return bool(held) and not meeting.get("notesMailedAt")


def _render(lines: list[dict]) -> str:
    return "".join(
        f"[{time.strftime('%H:%M', time.gmtime(x.get('at') or 0))}] {x['name']}: {x['text']}\n" for x in lines
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
    try:
        await _notes(room_id, meta)
    except Exception:
        # A failed attempt frees the claim at once, so the next terminal webhook retries it.
        await container_repo.release_owner_lifecycle_claim(lease, token=token)
        raise
    return True


async def _notes(room_id: str, meta: dict) -> None:
    occurrence = meta["meeting"].get("startAt")
    this_occurrence = lambda m: m.get("startAt") == occurrence  # noqa: E731 -- never mark the next one
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
    owner_id, meeting = meta["ownerId"], meta["meeting"]
    title = meeting.get("title") or "Meeting"
    target = await meeting_invites.output_room(owner_id, room_id)
    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    ceo = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    # The run is dispatched in the output room, so the CEO must sit there.
    if ceo and not meeting.get("notesRunId") and await messaging_repo.get_member(target, "agent", ceo):
        message = await meeting_invites._post_line(
            owner_id,
            target,
            f"meeting:{room_id}:{occurrence}:notes",
            f"Write the meeting notes for {title}: decisions, open questions, action items as Paperclip "
            "tasks for their owners, two paragraphs max; "
            + (
                "the transcript is attached"
                if meeting.get("transcriptAttachedAt")
                else "no transcript was captured; write notes from the room history"
            ),
            dispatch_targets=[{"agentId": ceo, "eventKind": "user_request"}],
        )
        if message:
            run_id = messaging_repo.run_id_for(ceo, message["messageId"])
            await meeting_invites._patch_meeting(room_id, {"notesRunId": run_id}, when=this_occurrence)
    if not await _mail_organizer(owner_id, room_id, target, meeting, title):
        raise RuntimeError("the organizer mail was not sent")
    await meeting_invites._patch_meeting(room_id, {"notesMailedAt": int(time.time())}, when=this_occurrence)


_MAIL_TRANSCRIPT_CHARS = 200_000


async def _transcript_text(owner_id: str, room_id: str, meeting: dict) -> str | None:
    if not meeting.get("transcriptId"):
        return None
    room_id = meeting.get("transcriptRoomId") or room_id  # the room it was stored for
    _, data = await run_in_thread(messaging.load_attachment, owner_id, room_id, meeting["transcriptId"])
    text = data.decode("utf-8", "replace")
    return text if len(text) <= _MAIL_TRANSCRIPT_CHARS else text[:_MAIL_TRANSCRIPT_CHARS] + "… (truncated)"


async def _mail_organizer(owner_id: str, room_id: str, target: str, meeting: dict, title: str) -> bool:
    """True when the notes are done: mailed, or deliberately not (no address, no organizer, a paused
    row, the owner's daily mail cap spent). False is a failed send: the marker stays unset and the
    terminal webhook's retry sends again. With a default room the mail links to it; without one
    (``target`` is the hidden meeting room itself) nobody can open the room, so the transcript
    travels in the mail."""
    # The member whose invite made or last updated the meeting: the ICS ORGANIZER is sender-written
    # and may be an outside host. Records from before ``invitedBy`` fall back to it.
    organizer = meeting.get("invitedBy") or meeting.get("organizer")
    address = await agent_email.team_address(owner_id)
    if not address or not organizer:
        return True
    if (await agent_email_repo.get_by_address(address) or {}).get("outbound_disabled"):
        return True
    if not await agent_mail_send.consume_cap(owner_id):  # the same per-owner daily cap as all agent mail
        logger.info("meeting room %s: daily mail cap spent, notes mail not sent", room_id)
        return True
    name = " ".join(title.split())
    if target != room_id:
        subject = f"Notes: {name}"
        body = f"The notes and transcript are in your Lightbulb room: {settings.FRONTEND_URL.rstrip('/')}/chat?room={target}"
    else:
        transcript = await _transcript_text(owner_id, target, meeting)
        subject = f"Transcript: {name}"
        body = f"Your Lightbulb agents joined {name}.\n\n" + (transcript or "No transcript was captured.")
    sent = await ses_email.send_ses_email(
        from_addr=address,
        to=organizer,
        subject=subject,
        body_text=body,
        headers=dict(AGENT_AUTHORED_HEADERS),
        kind="meeting_notes",
        tenant_name=await agent_mail_tenant.ensure_tenant(owner_id),
    )
    return sent is not None
