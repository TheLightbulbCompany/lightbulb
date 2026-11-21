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

from core.auth import AuthContext
from core.engine import api, contract
from core.repositories import messaging_repo
from core.services import messaging
from core.voice.meetings import meeting_bots, meeting_invites
from core.voice.meetings.records import inviting_member, record_transcription
from core.voice.room_lines import QuestionRef, agent_lines, assistant_name, latest_seq
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


async def _meeting(room_id: str, body: RoomBody) -> tuple[str, dict, dict]:
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != body.ownerId or not meta.get("meeting"):
        raise _problem(404, "meeting_not_found")
    return room_id, meta, meta["meeting"]


async def answer_said_question(owner_id: str, room_id: str, member: str, ref: QuestionRef | None, text: str,
                               key: str) -> bool:  # fmt: skip
    """The line said right after the voice said a Worker's question answers it, as `member`
    (`core.engine.api.answer_said_question`). False: the line goes to the Router as a message."""
    said = contract.QuestionRef(task_id=ref.taskId, qid=ref.qid) if ref else None
    return await api.answer_said_question(owner_id, room_id, member, said, text, key)


class StartIn(RoomBody):
    company: str = Field(default="", max_length=200)


@router.post("/internal/voice/meet/{room_id}/start")
async def start(room_id: str, body: StartIn) -> dict:
    """The assistant's name, the join announcement in the Meet's chat (once an occurrence), and `after`, the first
    `/events` cursor."""
    room_id, meta, meeting = await _meeting(room_id, body)
    name, bot = await assistant_name(body.ownerId), meeting.get("recallBotId")
    if bot:
        mine = await meeting_invites._patch_meeting(
            room_id, {"announced": True}, when=lambda m: m.get("recallBotId") == bot and not m.get("announced")
        )
        if mine is not None:
            try:  # best-effort: a refused chat line must not keep the worker from starting
                await meeting_bots.say(bot, announcement(name, body.company or "the team"))
            except Exception as e:  # noqa: BLE001 -- RecallError or a transport error alike
                logger.warning("meet room %s: announce failed %s", room_id, getattr(e, "code", type(e).__name__))
    return {
        "assistantName": name,
        "title": meeting.get("title") or "Meeting",
        "after": await latest_seq(room_id),
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
    room_id, _meta, meeting = await _meeting(room_id, body)
    member = inviting_member(meeting, await messaging_repo.list_members(room_id))
    if member is None:
        return {"posted": False, "reason": "no_inviter"}
    who = body.speakerName or ((meeting.get("roster") or {}).get(body.participantId) or {}).get("name") or "Someone"
    key = f"meet-ask:{room_id}:{meeting.get('inCallAt', 0)}:{body.participantId}:{body.text}"
    text = f"{who} said in the meeting: {body.text}"
    # an answer to the question just said is still the chat's line, but wakes nobody: the answer is the input
    answered = await answer_said_question(body.ownerId, room_id, member, body.answers, text, key)
    client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    if body.participantId:  # the meeting's own record of who spoke each line (by client id), written BEFORE the
        pid = body.participantId  # message exists so the Router can never run first; it reads it, never the model
        await meeting_invites._patch_meeting(
            room_id,
            lambda m: {"askSpeakers": {**dict(list((m.get("askSpeakers") or {}).items())[-49:]), client_id: pid}},
        )
    msg = await messaging.create_human_message(
        AuthContext(user_id=member, org_id=body.ownerId),
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.UUID(client_id),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="voice",
        **({"wake": False} if answered else {}),
    )
    out = {"posted": True, "messageId": str(msg.message_id)}
    return {**out, "answered": body.answers.qid} if answered else out


class EventsIn(RoomBody):
    after: int = Field(ge=0)


@router.post("/internal/voice/meet/{room_id}/events")
async def events(room_id: str, body: EventsIn) -> dict:
    """The agent messages the meeting's room got after `after`: the voice says each one in the meeting."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    return {**await agent_lines(room_id, body.after), "notTranscribed": meeting.get("notTranscribed") or []}


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


class TranscriptionIn(RoomBody):
    participantId: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=200)
    optOut: bool = True


@router.post("/internal/voice/meet/{room_id}/transcription")
async def transcription(room_id: str, body: TranscriptionIn) -> dict:
    """A person asked (by voice, addressing the assistant) not to be transcribed, or to be again. Kept on the
    meeting so a worker restart or a rejoin keeps it."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    pid, name = body.participantId, body.name.strip()
    if not (pid or name):
        raise _problem(400, "no_participant")
    return await record_transcription(room_id, meeting, pid, name, body.optOut)
