"""The meeting agent's backend (voice-agents spec §4.4, §4.4a, §5 "The meeting agent"): the huddle worker's
meeting mode (apps/huddle-agent `meet.py`, one Gemini Live session per meeting) calls these with the worker
secret and the room token.

The meeting's record lives on the room META's ``meeting`` map, where the owner's timeline reads it:
``asked`` [{t, who, q, a}], ``followUps`` [{text, status, taskId}], ``summary``.

Who may act: only the inviting member's own speech. A Meet speaker is the Recall participant the worker
heard; it counts as the member only when Recall marks them the Meet's host AND the invite's organizer is the
member who sent it (Google signs the host in as the organizer). Anyone else is a guest: their asks start
nothing that spends or sends -- the card waits for the member.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import Field

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import integration_reads, meeting_bots, meeting_invites, memory_api
from routers.huddles import RoomBody, _problem, _require_room_token, _room_id, _Worker
from routers.internal_automations import VoiceStartIn, start_voice_work
from routers.voice_phone import _bookings

router = APIRouter(dependencies=[_Worker])

CHAT_CHARS = 500  # Meet's chat limit (meeting_bots.say)
ANNOUNCE = (
    '{name} is in this meeting for {company}. It listens and speaks only when someone says "{name}, ..."; '
    'say "{name}, stop" to cut it off. Guests can ask; only {company}\'s members can approve work.'
)
GUEST_NOTE = "A guest asked for this in a meeting; it needs a member's OK before anything is spent or sent."


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
            await meeting_bots.say(bot, ANNOUNCE.format(name=name, company=body.company or "the team"))
    return {"assistantName": name, "title": meeting.get("title") or "Meeting"}


def member_speaking(meeting: dict, members: list[dict], participant_id: str) -> str | None:
    """The member whose own speech this is, or None (a guest). See the module docstring."""
    who = (meeting.get("roster") or {}).get(participant_id) or {}
    organizer, inviter = (meeting.get("organizer") or "").lower(), (meeting.get("invitedBy") or "").lower()
    if not (who.get("host") and organizer and organizer == inviter):
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
    repeat ask is "already on it" (the task id is derived from it). A guest's ask starts nothing."""
    room_id, meta, meeting = await _meeting(room_id, body)
    run_id = hashlib.sha256(
        f"{room_id}:{meeting.get('inCallAt', 0)}:{body.title.strip().lower()}".encode()
    ).hexdigest()[:32]
    if await messaging_repo.get_voice_run(body.ownerId, run_id):
        done = next((f for f in meeting.get("followUps") or [] if f.get("runId") == run_id), {})
        return {"already": True, "taskId": done.get("taskId")}
    member = member_speaking(meeting, await messaging_repo.list_members(room_id), body.participantId)
    if member is None:
        return {"started": False, "reason": "needs_member", "note": GUEST_NOTE}
    await messaging_repo.put_voice_run(
        body.ownerId, run_id, {"roomId": room_id, "requesterUserId": member, "agentId": None}
    )
    brief = f'Asked aloud in the meeting "{meeting.get("title") or "Meeting"}" by {body.speakerName or "a member"}.\n\n{body.brief}'
    out = await start_voice_work(
        body.ownerId, VoiceStartIn(run_id=run_id, call_id=room_id, room_id=room_id, title=body.title, brief=brief)
    )
    entry = {"text": body.title, "status": "working", "taskId": out["taskId"], "runId": run_id, "at": utc_now_iso()}
    await meeting_invites._patch_meeting(room_id, lambda m: {"followUps": [*(m.get("followUps") or []), entry]})
    return {"started": True, **out}


class ReadIn(RoomBody):
    kind: Literal["memory", "calls", "calendar"]
    question: str = Field(default="", max_length=1000)
    days: int = Field(default=7, ge=1, le=90)
    date: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


async def _calls(owner_id: str, days: int) -> dict:
    """Phone calls in the last `days`, by outcome (the call rooms' META.call records)."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    # ponytail: one META read per call room, capped at the newest 200; a calls index when volume needs it.
    rows = [r for r in await messaging_repo._query(messaging_repo.owner_pk(owner_id), begins_with="ROOM#")
            if str(r.get("name", "")).startswith("Call · ")]  # fmt: skip
    rows = sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)[:200]
    outcomes: dict[str, int] = {}
    for r in rows:
        call = ((await messaging_repo.get_room(r["roomId"])) or {}).get("call") or {}
        if call.get("startedAt", "") >= since:
            key = call.get("outcome") or call.get("status") or "unknown"
            outcomes[key] = outcomes.get(key, 0) + 1
    return {"days": days, "calls": sum(outcomes.values()), "byOutcome": outcomes}


@router.post("/internal/voice/meet/{room_id}/read")
async def read(room_id: str, body: ReadIn) -> dict:
    """The meeting's read tools: workspace memory (never a person's private scope: everyone hears it), call
    counts, and the assistant's own calendar."""
    room_id, _meta, _meeting_map = await _meeting(room_id, body)
    if body.kind == "memory":
        page = await memory_api.recall_page(
            body.ownerId, None, body.question or "recent decisions", budget_tokens=1500,
            audience={"kind": "room", "roomId": room_id},
        )  # fmt: skip
        return {"facts": [" ".join(r["content"].split())[:400] for r in page.get("results", [])[:6]]}
    if body.kind == "calls":
        return await _calls(body.ownerId, body.days)
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
    """One bounded read of a connected integration (integration_reads). The member's own speech only: the
    whole meeting hears the answer, and a guest must not read the owner's mail or payments aloud."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    if member_speaking(meeting, await messaging_repo.list_members(room_id), body.participantId) is None:
        return {"text": "Only a member can ask me to read the team's connected apps in a meeting."}
    return {"text": await integration_reads.read(body.ownerId, body.provider, body.query)}


class ChatIn(RoomBody):
    text: Annotated[str, Field(min_length=1, max_length=20000)]


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
