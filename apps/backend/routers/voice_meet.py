"""The meeting agent's backend (voice-agents spec §4.4, §4.4a, §5 "The meeting agent"): the huddle worker's
meeting mode (apps/huddle-agent `meet.py`, one Gemini Live session per meeting) calls these with the worker
secret and the room token.

The meeting's record lives on the room META's ``meeting`` map, where the owner's timeline reads it:
``asked`` [{t, who, q, a}], ``followUps`` [{text, status, taskId}], ``summary``.

Who may act: everyone in the meeting, with the powers of the inviting member (the organizer who sent the invite;
the host member when there is none). The speaker's name is kept for the record only. A task started from a meeting
carries ``confirmAll``: a spend, an outside send or a publish always gets a Needs-you card for the inviter.
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AfterValidator, Field

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import (
    integration_reads,
    livekit_huddles,
    meeting_bots,
    meeting_invites,
    memory_api,
    voice_approve,
)
from routers.huddles import RoomBody, _problem, _require_room_token, _room_id, _Worker
from routers.internal_automations import VoiceStartIn, start_voice_work
from routers.voice_phone import _TZ, _bookings

router = APIRouter(dependencies=[_Worker])
logger = logging.getLogger(__name__)

CHAT_CHARS = 500  # Meet's chat limit (meeting_bots.say)
ANNOUNCE = (
    '{name} is in this meeting for {company}. It listens and speaks only when someone says "{name}, ..."; '
    'say "{name}, stop" to cut it off. Anything that spends, sends or publishes waits for the inviting member\'s OK. '
    "This meeting is recorded and notes are saved to {company}'s workspace."
)


async def _meeting(room_id: str, body: RoomBody) -> tuple[str, dict, dict]:
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != body.ownerId or not meta.get("meeting"):
        raise _problem(404, "meeting_not_found")
    return room_id, meta, meta["meeting"]


async def assistant_name(owner_id: str) -> str:
    name = (((await messaging_repo.get_voice_agent(owner_id)) or {}).get("name") or "").strip()
    return name or "Lightbulb"


_warming: set[asyncio.Task] = set()  # held so the warm-up is not garbage-collected mid-flight


class StartIn(RoomBody):
    company: str = Field(default="", max_length=200)


@router.post("/internal/voice/meet/{room_id}/start")
async def start(room_id: str, body: StartIn) -> dict:
    """The assistant's name, and the join announcement in the Meet's chat, once an occurrence."""
    room_id, meta, meeting = await _meeting(room_id, body)
    _warming.add(t := asyncio.create_task(integration_reads.warm_google(body.ownerId)))
    t.add_done_callback(_warming.discard)
    name, bot = await assistant_name(body.ownerId), meeting.get("recallBotId")
    if bot:
        mine = await meeting_invites._patch_meeting(
            room_id, {"announced": True}, when=lambda m: m.get("recallBotId") == bot and not m.get("announced")
        )
        if mine is not None:
            try:  # best-effort: a refused chat line must not keep the worker from starting
                await meeting_bots.say(bot, ANNOUNCE.format(name=name, company=body.company or "the team"))
            except Exception as e:  # noqa: BLE001 -- RecallError or a transport error alike
                logger.warning("meet room %s: announce failed %s", room_id, getattr(e, "code", type(e).__name__))
    return {"assistantName": name, "title": meeting.get("title") or "Meeting"}


def inviting_member(meeting: dict, members: list[dict]) -> str | None:
    """The member whose authority the whole meeting acts under: the inviter, else the host member."""
    host = any(p.get("host") for p in (meeting.get("roster") or {}).values())
    if not (meeting.get("invitedBy") or host):
        return None
    return next((m["id"] for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)


class WorkIn(RoomBody):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    brief: Annotated[str, Field(min_length=1, max_length=8000)]
    participantId: str = Field(default="", max_length=128)
    speakerName: str = Field(default="", max_length=120)


@router.post("/internal/voice/meet/{room_id}/start-work", status_code=202)
async def start_work(room_id: str, body: WorkIn) -> dict:
    """An ask becomes a Worker task and a follow-up on the meeting. One title per occurrence is one task: a
    repeat ask is "already on it" (the task id is derived from it). Runs as the inviting member."""
    room_id, meta, meeting = await _meeting(room_id, body)
    run_id = hashlib.sha256(
        f"{room_id}:{meeting.get('inCallAt', 0)}:{body.title.strip().lower()}".encode()
    ).hexdigest()[:32]
    if await messaging_repo.get_voice_run(body.ownerId, run_id):
        done = next((f for f in meeting.get("followUps") or [] if f.get("runId") == run_id), {})
        return {"already": True, "taskId": done.get("taskId")}
    member = inviting_member(meeting, await messaging_repo.list_members(room_id))
    if member is None:
        return {"started": False, "reason": "no_inviter"}
    who = body.speakerName or ((meeting.get("roster") or {}).get(body.participantId) or {}).get("name") or ""
    await messaging_repo.put_voice_run(
        body.ownerId, run_id, {"roomId": room_id, "requesterUserId": member, "agentId": None}
    )
    brief = (
        f'Asked aloud in the meeting "{meeting.get("title") or "Meeting"}" by {who or "a participant"}.\n\n{body.brief}'
    )
    out = await start_voice_work(
        body.ownerId,
        VoiceStartIn(run_id=run_id, call_id=room_id, room_id=room_id, title=body.title, brief=brief),
        confirm_all=True,
    )
    entry = {
        "text": body.title,
        "status": "working",
        "taskId": out["taskId"],
        "runId": run_id,
        "who": who,
        "at": utc_now_iso(),
    }
    await meeting_invites._patch_meeting(room_id, lambda m: {"followUps": [*(m.get("followUps") or []), entry]})
    await _tile_phase(room_id, "drafting")
    return {"started": True, **out}


class ApproveIn(RoomBody):
    phrase: Literal["yes", "no", "undo"]
    hint: str = Field(default="", max_length=300)
    participantId: str = Field(default="", max_length=128)
    speakerName: str = Field(default="", max_length=120)


@router.post("/internal/voice/meet/{room_id}/approve")
async def approve(room_id: str, body: ApproveIn) -> dict:
    """Anyone's yes in the meeting is the inviting member's, and only queues their one-tap confirm."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    member = inviting_member(meeting, await messaging_repo.list_members(room_id))
    if member is None:
        return {"none": True}
    who = body.speakerName or ((meeting.get("roster") or {}).get(body.participantId) or {}).get("name") or ""
    speaker = {"user_id": member, "name": who, "where": meeting.get("title") or "the meeting"}
    return await voice_approve.approve(body.ownerId, "meet", speaker, body.phrase, body.hint, room_id=room_id)


class ReadIn(RoomBody):
    kind: Literal["memory", "calls", "calendar"]
    question: str = Field(default="", max_length=1000)
    days: Annotated[int, Field(le=90), AfterValidator(lambda d: max(d, 1))] = 7  # 0 or less: today
    date: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    participantId: str = Field(default="", max_length=128)
    from_: str = Field(default="", alias="from", pattern=r"^(\d{4}-\d{2}-\d{2})?$")  # call counts over local days
    to: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


def _started(call: dict) -> datetime:
    try:
        at = datetime.fromisoformat(call.get("startedAt") or "")
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


async def _calls(owner_id: str, days: int, frm: str = "", to: str = "") -> dict:
    """Phone calls in the last `days`, or on the business's local days `frm`..`to` inclusive, by outcome (the call
    rooms' META.call records)."""
    since, until = datetime.now(UTC) - timedelta(days=days), datetime.max.replace(tzinfo=UTC)
    if frm:
        to = to or frm
        since = datetime.fromisoformat(frm).replace(tzinfo=_TZ)
        until = datetime.fromisoformat(to).replace(tzinfo=_TZ) + timedelta(days=1)
    # ponytail: one META read per call room, capped at the newest 200; a calls index when volume needs it.
    rows = [r for r in await messaging_repo._query(messaging_repo.owner_pk(owner_id), begins_with="ROOM#")
            if str(r.get("name", "")).startswith("Call · ")]  # fmt: skip
    rows = sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)[:200]
    outcomes: dict[str, int] = {}
    for r in rows:
        call = ((await messaging_repo.get_room(r["roomId"])) or {}).get("call") or {}
        if since <= _started(call) < until:  # startedAt carries the business's offset: compare as times
            key = call.get("outcome") or call.get("status") or "unknown"
            outcomes[key] = outcomes.get(key, 0) + 1
    span = {"from": frm, "to": to} if frm else {"days": days}
    return {**span, "calls": sum(outcomes.values()), "byOutcome": outcomes}


@router.post("/internal/voice/meet/{room_id}/read")
async def read(room_id: str, body: ReadIn) -> dict:
    """The meeting's read tools: workspace memory (never a person's private scope: everyone hears it), call
    counts, and the assistant's own calendar."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    if body.kind == "memory":
        page = await memory_api.recall_page(
            body.ownerId, None, body.question or "recent decisions", budget_tokens=1500,
            audience={"kind": "room", "roomId": room_id},
        )  # fmt: skip
        return {"facts": [" ".join(r["content"].split())[:400] for r in page.get("results", [])[:6]]}
    if body.kind == "calls":
        return await _calls(body.ownerId, body.days, body.from_, body.to)
    rows = await _bookings(body.ownerId, body.date)
    return {"bookings": [{"start": b["sk"].removeprefix("VOICEBOOK#"), "service": b.get("service"),
                          "name": b.get("name")} for b in rows[:50]]}  # fmt: skip


class IntegrationIn(RoomBody):
    provider: Annotated[str, Field(min_length=1, max_length=40)]
    query: str = Field(default="", max_length=300)
    participantId: str = Field(default="", max_length=128)


@router.post("/internal/voice/meet/{room_id}/integrations")
async def integrations(room_id: str, body: RoomBody) -> dict:
    await _meeting(room_id, body)
    return {"connected": await integration_reads.connected(body.ownerId)}


@router.post("/internal/voice/meet/{room_id}/integration-read")
async def integration_read(room_id: str, body: IntegrationIn) -> dict:
    """One bounded read of a connected integration (integration_reads)."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    out = await integration_reads.read(body.ownerId, body.provider, body.query)
    return out if isinstance(out, dict) else {"text": out}


class ChatIn(RoomBody):
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    participantId: str = Field(default="", max_length=128)


@router.post("/internal/voice/meet/{room_id}/chat")
async def chat(room_id: str, body: ChatIn) -> dict:
    """A short line goes to the Meet's chat as it is; a draft or a long answer is posted to the meeting's room
    and the chat gets its first line and a link."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    bot = meeting.get("recallBotId")
    if not bot:
        raise _problem(409, "no_bot")
    if len(body.text) <= CHAT_CHARS:
        await meeting_bots.say(bot, body.text)
        return {"sent": "chat"}
    key = f"meeting:{bot}:draft:{hashlib.sha256(body.text.encode()).hexdigest()[:16]}"
    await meeting_invites._post_line(body.ownerId, room_id, key, body.text)
    target = await meeting_invites.output_room(body.ownerId, room_id)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?room={target}"
    head = body.text.strip().splitlines()[0][:200]
    await meeting_bots.say(bot, f"{head} ... Full text: {link}")
    return {"sent": "link", "link": link}


class EndIn(RoomBody):
    asked: list[dict] = Field(default_factory=list, max_length=200)
    summary: str = Field(default="", max_length=4000)


@router.post("/internal/voice/meet/{room_id}/end")
async def end(room_id: str, body: EndIn) -> dict:
    """What the assistant was asked and answered, kept for the owner's timeline."""
    room_id, _meta, _meeting_map = await _meeting(room_id, body)
    asked = [
        {"t": int(a.get("t") or 0), "who": str(a.get("who") or "")[:120], "q": str(a.get("q") or "")[:1000],
         "a": str(a.get("a") or "")[:2000]}
        for a in body.asked
    ]  # fmt: skip
    await meeting_invites._patch_meeting(room_id, {"asked": asked, "summary": body.summary})
    return {"ok": True}


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
    it; a newly done one is said in the Meet's chat with its link while the bot is still in the call. A voice
    trigger's call id is the room it was asked in; a phone call's room has no meeting, so nothing is written.
    Best-effort: a status change never fails over this."""
    parts, status = str(task.get("triggerId") or "").split(":"), _FOLLOW_UP.get(to)
    if parts[0] != "voice" or len(parts) != 3 or status is None:
        return
    mine = lambda f: f.get("taskId") == task["taskId"]  # noqa: E731
    try:
        meeting = await meeting_invites._patch_meeting(
            parts[1],
            lambda m: {"followUps": [{**f, "status": status} if mine(f) else f for f in m.get("followUps") or []]},
            when=lambda m: any(mine(f) and f.get("status") != status for f in m.get("followUps") or []),
        )  # None: no such follow-up, or already this status (a retried Activity says nothing twice)
        if meeting and status == "done" and meeting.get("status") == "in_call" and meeting.get("recallBotId"):
            link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=task&taskId={task['taskId']}"
            await meeting_bots.say(meeting["recallBotId"], f"Draft ready: {task.get('title') or 'Task'} — {link}")
            await _tile_phase(parts[1], "page_ready")
        elif meeting and status != "done" and meeting.get("status") == "in_call":
            await _tile_phase(parts[1], "drafting" if status == "working" else "")
    except Exception:  # noqa: BLE001 -- RecallError, a lost CAS race or a transport error alike
        logger.warning("meet follow-up not updated task=%s to=%s", task.get("taskId"), to, exc_info=True)
