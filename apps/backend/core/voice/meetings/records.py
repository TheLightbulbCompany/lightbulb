"""The meeting's record on the room META (``meeting``): whose authority it acts under, its follow-ups, the result
links said in the Meet's chat, and who is not transcribed."""

import asyncio
import logging

from core.config import settings
from core.engine import api
from core.voice import livekit_huddles
from core.voice.meetings import meeting_bots, meeting_invites

# The name these lines always logged under (routers/voice_meet.py): the W85 alarm matches loggers `routers.voice*`.
# Kept on the old router name: alarm W85 (observability-stack.ts) filters on loggers named routers.voice*.
logger = logging.getLogger("routers.voice_meet")


def inviting_member(meeting: dict, members: list[dict]) -> str | None:
    """The member whose authority the whole meeting acts under: the inviter, else the host member."""
    host = any(p.get("host") for p in (meeting.get("roster") or {}).values())
    if not (meeting.get("invitedBy") or host):
        return None
    return next((m["id"] for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)


_FOLLOW_UP = {"working": "working", "waiting": "needs_you", "failed": "needs_you", "idle": "done", "resolved": "done"}


_clears: dict[str, asyncio.Task] = {}


async def _tile_phase(room_id: str, phase: str) -> None:
    """Drafting while the task works; page_ready for 60 s once its link is posted. A newer phase cancels the clear."""
    if old := _clears.pop(room_id, None):
        old.cancel()
    await livekit_huddles.set_meet_phase(room_id, phase)
    if phase == "page_ready":

        async def clear() -> None:
            await asyncio.sleep(60)
            _clears.pop(room_id, None)
            await livekit_huddles.set_meet_phase(room_id, "")

        _clears[room_id] = asyncio.create_task(clear())  # ponytail: lost on a restart; the next phase overwrites it


async def task_moved(task: dict, to: str) -> None:
    """A voice task changed status (activities._announce). When it is a meeting's follow-up, the follow-up tracks
    it; a newly done one is said in the Meet's chat with its link while the bot is still in the call. The canonical
    meeting origin and room audience identify it; the task workspace must own that room.
    Best-effort: a status change never fails over this."""
    audience, status = task.get("audience") or {}, _FOLLOW_UP.get(to)
    room_id = audience.get("roomId")
    owner_id = str(task.get("pk") or "").removeprefix("WS#")
    if (
        task.get("origin") != "meeting"
        or not str(task.get("pk") or "").startswith("WS#")
        or not owner_id
        or status is None
    ):
        return
    mine = lambda f: f.get("taskId") == task["taskId"]  # noqa: E731

    def updated(entry: dict) -> dict:
        return {**entry, "followUps": [{**f, "status": status} if mine(f) else f for f in entry.get("followUps") or []]}

    def changed(m: dict) -> bool:
        return any(
            mine(f) and f.get("status") != status
            for entry in [m, *(m.get("history") or [])]
            for f in entry.get("followUps") or []
        )

    try:
        from core.voice.meetings import meeting_followups

        if await meeting_followups.record_task(owner_id, {**task, "status": to}):
            return  # completion work belongs to its original notes, never a newly active Meet
        if audience.get("kind") != "room" or not room_id:
            return  # live provider delivery remains room-only
        conv = await api.get_conversation(owner_id, room_id)
        if not conv or not conv.annotations.get("meeting"):
            return
        current_changed = False

        def lay(m: dict) -> dict:
            nonlocal current_changed
            current_changed = any(mine(f) and f.get("status") != status for f in m.get("followUps") or [])
            return {"followUps": updated(m)["followUps"], "history": [updated(h) for h in m.get("history") or []]}

        meeting = await meeting_invites._patch_meeting(
            owner_id, room_id, lay, when=changed
        )  # a retried Activity changes nothing and says nothing twice
        if (
            meeting
            and current_changed
            and status == "done"
            and meeting.get("status") == "in_call"
            and meeting.get("recallBotId")
            and await _result_occurrence(owner_id, task, room_id, meeting)
        ):
            link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=task-detail&taskId={task['taskId']}"
            await meeting_bots.say(meeting["recallBotId"], f"Draft ready: {task.get('title') or 'Task'} — {link}")
            await _tile_phase(room_id, "page_ready")
        elif (
            meeting
            and current_changed
            and status != "done"
            and meeting.get("status") == "in_call"
            and await _result_occurrence(owner_id, task, room_id, meeting)
        ):
            await _tile_phase(room_id, "drafting" if status == "working" else "")
    except Exception:  # noqa: BLE001 -- RecallError, a lost CAS race or a transport error alike
        logger.warning("meet follow-up not updated task=%s to=%s", task.get("taskId"), to, exc_info=True)


def task_link(task_id: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=task-detail&taskId={task_id}"


async def _result_occurrence(owner_id: str, task: dict, room_id: str, meeting: dict) -> tuple[str, int | float] | None:
    """Provider delivery needs positive canonical provenance; an app result needs no live bot. `meeting`: the
    record of `owner_id`'s room `room_id`."""
    if task.get("origin") != "meeting" or task.get("pk") != f"WS#{owner_id}":
        return None
    trigger = await api.get_trigger(owner_id, task.get("triggerId")) if task.get("triggerId") else None
    ref = (trigger or {}).get("ref") or {}
    if ref.get("roomId") != room_id or ref.get("origin") != "meeting" or ref.get("meetingFollowups"):
        return None
    bot, started = ref.get("meetingBotId"), ref.get("meetingInCallAt")
    return (
        (bot, started)
        if bot and started and bot == meeting.get("recallBotId") and started == meeting.get("inCallAt")
        else None
    )


async def with_link(owner_id: str, room_id: str, task: dict, line: str) -> str:
    """A finished thread that made something (an output: a document, a page) and came from a meeting's room: its
    result line in that room carries the link, and the line goes to the Meet's chat while the bot is in the call, once
    a task (8 Nov prod Meet re-run: "put the link in chat" was never delivered: the result line had only a task card,
    and `task_moved` says a link only for `followUps`, which nothing writes since the one-Router design). Any other
    room, or a thread with no output (an answer): the line as it is."""
    if not task.get("outputs"):
        return line
    tid, linked = task["taskId"], f"{line} {task_link(task['taskId'])}"
    try:
        conv = await api.get_conversation(owner_id, room_id)
        meeting = conv.annotations.get("meeting") if conv else None
    except Exception:  # noqa: BLE001 -- the line must still post
        logger.warning("meet room %s: room not read for the result link task=%s", room_id, tid, exc_info=True)
        return line
    if not meeting:
        return line
    try:
        occurrence = await _result_occurrence(owner_id, task, room_id, meeting)
        if occurrence is None:
            return linked
        bot, started = occurrence
        fresh = await meeting_invites._patch_meeting(
            owner_id,
            room_id,
            lambda m: {"linksSaid": [*(m.get("linksSaid") or []), tid]},
            when=lambda m: (
                m.get("status") == "in_call"
                and m.get("recallBotId") == bot
                and m.get("inCallAt") == started
                and tid not in (m.get("linksSaid") or [])
            ),
        )  # None: not in the call, or already said (a retried Activity)
        if fresh is not None:
            await meeting_bots.say(fresh["recallBotId"], linked)
            await _tile_phase(room_id, "page_ready")
    except Exception:  # noqa: BLE001 -- a lost read, CAS race or RecallError: the room line still posts
        logger.warning("meet room %s: result link not said task=%s", room_id, tid, exc_info=True)
    return linked


async def end_deleted(owner_id: str, room_id: str) -> None:
    """A member deleted the meeting's room: the meeting stops. Its bot leaves the call, or its booking is cancelled
    (`meeting_bots.leave`), and the Meet's LiveKit room closes; Recall's terminal status then meters the call to that
    moment (`routers.meetings._on_status`). Never raises: the delete already happened."""
    try:
        conv = await api.get_conversation(owner_id, room_id, include_deleted=True)
        meeting = (conv.annotations.get("meeting") if conv else None) or {}
        if not conv or not conv.deleted or not meeting:
            return
        if (bot := meeting.get("recallBotId")) and meeting.get("status") not in ("ended", "bot_failed", "cancelled"):
            await meeting_bots.leave(bot)
        await livekit_huddles.close_room(room_id, meet=True)
    except Exception:  # noqa: BLE001 -- a RecallError or a transport error: Recall's own leave reaps the bot
        logger.warning("meeting room %s: deleted, but its bot was not pulled", room_id, exc_info=True)


def _same(entry: dict, pid: str, name: str) -> bool:
    return bool((pid and entry.get("id") == pid) or (name and entry.get("name", "").lower() == name.lower()))


async def record_transcription(
    owner_id: str, room_id: str, meeting: dict, pid: str, name: str, opt_out: bool = True
) -> dict:
    """Keep (or lift) one person's exclusion on the meeting; the Meet chat gets one line saying so (best-effort).
    The one writer: the worker route below and the Router's `meeting_stop_transcribing` both come here."""

    def change(m: dict) -> dict:
        rest = [e for e in m.get("notTranscribed") or [] if not _same(e, pid, name)]
        return {"notTranscribed": [*rest, {"id": pid, "name": name}] if opt_out else rest}

    meeting = await meeting_invites._patch_meeting(owner_id, room_id, change) or meeting
    if bot := meeting.get("recallBotId"):
        who = name or "you"
        line = (f"OK {who}: your voice is excluded from the assistant and its transcript. The meeting recording continues." if opt_out
                else f"OK {who}: you are transcribed again.")  # fmt: skip
        try:
            await meeting_bots.say(bot, line)
        except Exception as e:  # noqa: BLE001 -- the opt-out stands without the chat line
            logger.warning("meet room %s: opt-out chat failed %s", room_id, getattr(e, "code", type(e).__name__))
    return {"notTranscribed": meeting.get("notTranscribed") or []}
