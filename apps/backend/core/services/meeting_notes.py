"""After a meeting: its transcript lands in the meeting room as ``transcript.txt`` (never as
messages), one CEO run writes the notes there, and the organizer gets one mail with the room link.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import agent_email, agent_mail_tenant, meeting_invites, messaging, ses_email
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS

logger = logging.getLogger(__name__)

_BUFFER_TTL_S = 3 * 3600
_WAIT_S, _POLL_S = 20, 2
# ponytail: one backend task holds a meeting's chunks in memory until the final one, so a deploy
# mid-flush loses the buffer; upgrade: the worker re-sends every line on its final post.
_buffers: dict[str, tuple[float, dict[int, dict]]] = {}


def _render(lines: list[dict]) -> str:
    return "".join(
        f"[{time.strftime('%H:%M', time.gmtime(x.get('at') or 0))}] {x['name']}: {x['text']}\n" for x in lines
    )


async def on_transcript(room_id: str, lines: list[dict], final: bool) -> None:
    """Buffer one chunk of a meeting's transcript (keyed by mark, so a retried chunk is not
    doubled); on the final chunk attach the whole thing to the room, once."""
    now = time.time()
    for stale in [r for r, (at, _) in _buffers.items() if now - at > _BUFFER_TTL_S]:
        del _buffers[stale]
    _buffers.setdefault(room_id, (now, {}))[1].update((int(x["mark"]), x) for x in lines)
    if not final:
        return
    meta = await messaging_repo.get_room(room_id) or {}
    meeting = meta.get("meeting") or {}
    if meeting.get("transcriptAttachedAt") or not meta.get("ownerId"):
        _buffers.pop(room_id, None)
        return
    owner_id = meta["ownerId"]
    buffered = _buffers[room_id][1]
    data = _render([buffered[m] for m in sorted(buffered)]).encode()
    attachment = await run_in_thread(
        messaging.store_attachment, owner_id, room_id, "transcript.txt", "text/plain", data
    )
    await meeting_invites._post_line(
        owner_id,
        room_id,
        f"meeting:{room_id}:transcript",
        "Transcript attached.",
        attachments=[attachment.model_dump(mode="json", by_alias=True)],
    )
    await meeting_invites._patch_meeting(room_id, {"transcriptAttachedAt": int(time.time())})
    _buffers.pop(room_id, None)


async def on_ended(room_id: str) -> None:
    """One notes run and one organizer mail per meeting. Fire-and-forget: never raises."""
    try:
        await _on_ended(room_id)
    except Exception:  # noqa: BLE001
        put_metric("meeting.notes.failed")
        logger.warning("meeting room %s: notes failed", room_id, exc_info=True)


async def _on_ended(room_id: str) -> None:
    meta = await messaging_repo.get_room(room_id) or {}
    if not meta.get("meeting") or not await messaging_repo.set_meeting_field_if_absent(
        room_id, "notesRunId", "pending"
    ):
        return  # not a meeting, or another end path already took the notes
    # The worker's final transcript POST and Recall's call_ended race: give the transcript a moment.
    for _ in range(_WAIT_S // _POLL_S):
        if (meta.get("meeting") or {}).get("transcriptAttachedAt"):
            break
        await asyncio.sleep(_POLL_S)
        meta = await messaging_repo.get_room(room_id) or meta
    owner_id, meeting = meta["ownerId"], meta["meeting"]
    title = meeting.get("title") or "Meeting"
    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    ceo = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    if ceo:
        message = await meeting_invites._post_line(
            owner_id,
            room_id,
            f"meeting:{room_id}:notes",
            f"Write the meeting notes for {title}: decisions, open questions, action items as Paperclip "
            "tasks for their owners, two paragraphs max; the transcript is attached",
            dispatch_targets=[{"agentId": ceo, "eventKind": "user_request"}],
        )
        if message:
            run_id = messaging_repo.run_id_for(ceo, message["messageId"])
            await meeting_invites._patch_meeting(room_id, {"notesRunId": run_id})
    await _mail_organizer(owner_id, room_id, meeting.get("organizer"), title)


async def _mail_organizer(owner_id: str, room_id: str, organizer: str | None, title: str) -> None:
    address = await agent_email.team_address(owner_id)
    if not address or not organizer:
        return
    if (await agent_email_repo.get_by_address(address) or {}).get("outbound_disabled"):
        return
    link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?room={room_id}"
    await ses_email.send_ses_email(
        from_addr=address,
        to=organizer,
        subject=f"Notes: {' '.join(title.split())}",
        body_text=f"The notes and transcript are in your Lightbulb room: {link}",
        headers=dict(AGENT_AUTHORED_HEADERS),
        kind="meeting_notes",
        tenant_name=await agent_mail_tenant.ensure_tenant(owner_id),
    )
