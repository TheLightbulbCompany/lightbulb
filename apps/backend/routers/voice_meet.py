"""The meeting agent's backend (one-brain plan, founder ruling 8 Nov 2026, option A): the huddle worker's meeting
mode (apps/huddle-agent `meet.py`) is ears and a mouth for the meeting room's Router. It calls these with the worker
secret and the room token: `/ask` posts a line addressed to the assistant as a message in the meeting's room (a
Router room, admission), and `/events` hands back the agent messages that room gets, which the voice says.

The meeting's record lives on the room META's ``meeting`` map, where the owner's timeline reads it:
``asked`` [{t, who, q, a}], ``followUps`` [{text, status, taskId}], ``summary``, ``linksSaid`` [taskId].

Who may act: everyone in the meeting, with the powers of the inviting member (the organizer who sent the invite;
the host member when there is none); the line is theirs, naming who said it. A task started from a meeting carries
``confirmAll`` (admission's ``origin: meeting``): a spend, an outside send or a publish always gets a Needs-you card.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter
from pydantic import Field

from core.engine import client as engine
from core.engine.contract import ConversationOut, LineIn, Principal
from core.engine.contract import QuestionRef as SaidQuestion
from core.voice.meetings import meeting_bots, meeting_invites
from core.voice.meetings.records import inviting_member, record_transcription
from core.voice.room_lines import QuestionRef, assistant_name
from core.voice.worker_api import RoomBody, _problem, _require_room_token, _room_id, _Worker

router = APIRouter(dependencies=[_Worker])
logger = logging.getLogger(__name__)

ANNOUNCE = (
    "{name} is here for {company}. Say its name to wake it; while awake it follows the conversation. "
    "Spending, sending or publishing requires the inviter's approval in the app. "
    "This meeting is recorded and notes are saved to your workspace. "
    'Say "{name}, stop transcribing me" to exclude your voice from the assistant. The meeting recording continues.'
)


def announcement(name: str, company: str) -> str:
    return ANNOUNCE.format(name=name[:40], company=company[:70])


async def _meeting(room_id: str, body: RoomBody, *, with_members: bool = False) -> tuple[str, ConversationOut, dict]:
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    conv = await engine.http(engine.get_conversation(body.ownerId, room_id, with_members=with_members))
    if not conv or not conv.annotations.get("meeting"):
        raise _problem(404, "meeting_not_found")
    return room_id, conv, conv.annotations["meeting"]


class StartIn(RoomBody):
    company: str = Field(default="", max_length=200)


@router.post("/internal/voice/meet/{room_id}/start")
async def start(room_id: str, body: StartIn) -> dict:
    """The assistant's name, the join announcement in the Meet's chat (once an occurrence), and `after`, the first
    `/events` cursor."""
    room_id, _conv, meeting = await _meeting(room_id, body)
    name, bot = await assistant_name(body.ownerId), meeting.get("recallBotId")
    if bot:
        mine = await meeting_invites._patch_meeting(
            body.ownerId,
            room_id,
            {"announced": True},
            when=lambda m: m.get("recallBotId") == bot and not m.get("announced"),
        )
        if mine is not None:
            try:  # best-effort: a refused chat line must not keep the worker from starting
                await meeting_bots.say(bot, announcement(name, body.company or "the team"))
            except Exception as e:  # noqa: BLE001 -- RecallError or a transport error alike
                logger.warning("meet room %s: announce failed %s", room_id, getattr(e, "code", type(e).__name__))
    return {
        "assistantName": name,
        "title": meeting.get("title") or "Meeting",
        "after": (await engine.http(engine.read_lines(body.ownerId, room_id, None)))["after"],
        "notTranscribed": meeting.get("notTranscribed") or [],
    }


class AskIn(RoomBody):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    participantId: str = Field(default="", max_length=128)
    speakerName: str = Field(default="", max_length=120)
    answers: QuestionRef | None = None  # the Worker question the voice said last: this line answers it


@router.post("/internal/voice/meet/{room_id}/ask")
async def ask(room_id: str, body: AskIn) -> dict:
    """A line said to the assistant by name becomes a message in the meeting's room, as the inviting member's
    (`via: voice`), naming who said it: it wakes the room's Router like a typed message."""
    room_id, conv, meeting = await _meeting(room_id, body, with_members=True)
    member = inviting_member(meeting, conv.members)
    if member is None:
        return {"posted": False, "reason": "no_inviter"}
    who = body.speakerName or ((meeting.get("roster") or {}).get(body.participantId) or {}).get("name") or "Someone"
    key = f"meet-ask:{room_id}:{meeting.get('inCallAt', 0)}:{body.participantId}:{body.text}"
    if body.participantId:
        # who spoke each line (by client id), on the meeting's own record BEFORE the message exists, so the Router
        # can never run first; it reads it, never the model
        pid, client_id = body.participantId, str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        await meeting_invites._patch_meeting(
            body.ownerId,
            room_id,
            lambda m: {"askSpeakers": {**dict(list((m.get("askSpeakers") or {}).items())[-49:]), client_id: pid}},
        )
    # an answer to the question just said is still the chat's line, but wakes nobody: the answer is the input
    line = await engine.http(
        engine.append_line(
            LineIn(
                tenant_id=body.ownerId,
                conversation_id=room_id,
                principal=Principal(kind="member", id=member),
                text=f"{who} said in the meeting: {body.text}",
                idempotency_key=key,
                via="voice",
                answers=SaidQuestion(task_id=body.answers.taskId, qid=body.answers.qid) if body.answers else None,
            )
        )
    )
    out = {"posted": True, "messageId": line.message_id}
    return {**out, "answered": body.answers.qid} if line.answered else out


class EventsIn(RoomBody):
    after: int = Field(ge=0)


@router.post("/internal/voice/meet/{room_id}/events")
async def events(room_id: str, body: EventsIn) -> dict:
    """The agent messages the meeting's room got after `after`: the voice says each one in the meeting."""
    room_id, _conv, meeting = await _meeting(room_id, body)
    lines = await engine.http(engine.read_lines(body.ownerId, room_id, body.after))
    return {**lines, "notTranscribed": meeting.get("notTranscribed") or []}


class EndIn(RoomBody):
    asked: list[dict] = Field(default_factory=list, max_length=200)
    summary: str = Field(default="", max_length=4000)


@router.post("/internal/voice/meet/{room_id}/end")
async def end(room_id: str, body: EndIn) -> dict:
    """What the assistant was asked and answered, kept for the owner's timeline."""
    room_id, _conv, _meeting_map = await _meeting(room_id, body)
    asked = [
        {"t": int(a.get("t") or 0), "who": str(a.get("who") or "")[:120], "q": str(a.get("q") or "")[:1000],
         "a": str(a.get("a") or "")[:2000]}
        for a in body.asked
    ]  # fmt: skip
    await meeting_invites._patch_meeting(body.ownerId, room_id, {"asked": asked, "summary": body.summary})
    return {"ok": True}


class TranscriptionIn(RoomBody):
    participantId: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=200)
    optOut: bool = True


@router.post("/internal/voice/meet/{room_id}/transcription")
async def transcription(room_id: str, body: TranscriptionIn) -> dict:
    """A person asked (by voice, addressing the assistant) not to be transcribed, or to be again. Kept on the
    meeting so a worker restart or a rejoin keeps it."""
    room_id, _conv, meeting = await _meeting(room_id, body)
    pid, name = body.participantId, body.name.strip()
    if not (pid or name):
        raise _problem(400, "no_participant")
    return await record_transcription(body.ownerId, room_id, meeting, pid, name, body.optOut)
