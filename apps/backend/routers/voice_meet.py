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

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext
from core.config import settings
from core.repositories import messaging_repo
from core.services import livekit_huddles, meeting_bots, meeting_invites, messaging
from routers.huddles import RoomBody, _problem, _require_room_token, _room_id, _Worker

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


async def assistant_name(owner_id: str) -> str:
    name = (((await messaging_repo.get_voice_agent(owner_id)) or {}).get("name") or "").strip()
    return name or "Lightbulb"


async def latest_seq(room_id: str) -> int:
    """The room's newest message seq: the first `/events` cursor, so a talk says only what comes after it."""
    rows = await messaging_repo.list_messages_before(room_id, 10**19, 1)
    return int(rows[-1]["messageSeq"]) if rows else 0


def _question_ref(row: dict) -> dict | None:
    """{taskId, qid} of a Worker's question line (task_questions.relay_question posts it in the origin thread)."""
    if row.get("kind") != "question":
        return None
    ref = next((t for t in row.get("taskRefs") or [] if t.get("qid") and t.get("taskId")), None)
    return {"taskId": ref["taskId"], "qid": ref["qid"]} if ref else None


async def agent_lines(room_id: str, after: int) -> dict:
    """The agent messages in the room's main chat after seq `after`, oldest first, and the next cursor. A thread's
    lines and progress lines are not said, except a Worker's question in a thread this room started (with its
    `question` ref: the next line said to the voice answers it, `answer_said_question`); the meeting never heard
    one and the task waited on it (8 Nov prod Meet). The voice says these word for word (huddle-agent mouth.py).
    `inReplyTo`: the human message a line answers (the voice matches it to that spoken ask); `action`: what the voice
    does with it (the phone's "transfer")."""
    from core.repositories import questions_repo

    rows, _ = await messaging_repo.list_messages(room_id, after, 100)
    said, owner = [], None
    for r in rows:
        text, question = r.get("editedText") or r.get("text") or "", _question_ref(r)
        if (r.get("author") or {}).get("kind") != "agent" or r.get("progress") or r.get("deletedAt"):
            continue
        if question:  # answered or closed (on its card, or by voice) before the voice got to it: never said
            owner = owner or ((await messaging_repo.get_room(room_id)) or {}).get("ownerId")
            q = await questions_repo.get_question(owner, question["taskId"], question["qid"]) if owner else None
            if (q or {}).get("state") != "open":
                continue
        if text.strip() and (question or not r.get("threadRootMessageId")):
            said.append({"id": r["messageId"], "seq": int(r["messageSeq"]), "text": text,
                         "waiting": bool(r.get("voiceWaiting", r.get("taskRefs") and r.get("kind") != "task_line")) if not question else False,
                         **({"taskIds": r.get("voiceTaskIds") or [ref["taskId"] for ref in r["taskRefs"]]} if r.get("voiceTaskIds") or r.get("taskRefs") else {}),
                         **({"question": question} if question else {}),
                         **{k: r[k] for k in ("inReplyTo", "action") if r.get(k)}})  # fmt: skip
    return {"messages": said, "after": int(rows[-1]["messageSeq"]) if rows else after}


class QuestionRef(BaseModel):
    taskId: str = Field(min_length=1, max_length=64)
    qid: str = Field(min_length=1, max_length=64)


async def answer_said_question(owner_id: str, room_id: str, member: str, ref: QuestionRef | None, text: str,
                               key: str) -> bool:  # fmt: skip
    """The line said right after the voice said a Worker's question answers it, as `member`, through the card's own
    path (routers/questions.submit_answer): a message in the room never answers a Question (task_questions.py).
    False (the line then goes to the Router as a message): no ref, a question this room never got, or a refused
    answer (closed, not an answerer, an approval: a spoken yes never approves)."""
    if ref is None:
        return False
    from routers import questions as question_routes

    rows = await messaging_repo.list_messages_before(room_id, 10**19, 100)
    if not any(_question_ref(r) == ref.model_dump() for r in rows):
        return False
    body = question_routes.AnswerBody(option_id="answer", text=text[:4000],
                                      client_answer_id=uuid.uuid5(uuid.NAMESPACE_URL, key).hex)  # fmt: skip
    try:
        await question_routes.submit_answer(owner_id, ref.taskId, ref.qid, member, body)
    except HTTPException as e:
        logger.info("voice room %s: question %s not answered by voice (%s)", room_id, ref.qid, e.detail)
        return False
    return True


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


def inviting_member(meeting: dict, members: list[dict]) -> str | None:
    """The member whose authority the whole meeting acts under: the inviter, else the host member."""
    host = any(p.get("host") for p in (meeting.get("roster") or {}).values())
    if not (meeting.get("invitedBy") or host):
        return None
    return next((m["id"] for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)


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
    msg = await messaging.create_human_message(
        AuthContext(user_id=member, org_id=body.ownerId),
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
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
    room_id, _meta, _meeting_map = await _meeting(room_id, body)
    return await agent_lines(room_id, body.after)


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
    it; a newly done one is said in the Meet's chat with its link while the bot is still in the call. The canonical
    meeting origin and room audience identify it; the task workspace must own that room.
    Best-effort: a status change never fails over this."""
    audience, status = task.get("audience") or {}, _FOLLOW_UP.get(to)
    room_id = audience.get("roomId")
    owner_id = str(task.get("pk") or "").removeprefix("WS#")
    if (
        task.get("origin") != "meeting"
        or audience.get("kind") != "room"
        or not room_id
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
        meta = (await messaging_repo.get_room(room_id)) or {}
        if meta.get("ownerId") != owner_id or not meta.get("meeting"):
            return
        current_changed = False

        def lay(m: dict) -> dict:
            nonlocal current_changed
            current_changed = any(mine(f) and f.get("status") != status for f in m.get("followUps") or [])
            return {"followUps": updated(m)["followUps"], "history": [updated(h) for h in m.get("history") or []]}

        meeting = await meeting_invites._patch_meeting(
            room_id,
            lay,
            when=changed,
        )  # a retried Activity changes nothing and says nothing twice
        if (
            meeting
            and current_changed
            and status == "done"
            and meeting.get("status") == "in_call"
            and meeting.get("recallBotId")
        ):
            link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=task&taskId={task['taskId']}"
            await meeting_bots.say(meeting["recallBotId"], f"Draft ready: {task.get('title') or 'Task'} — {link}")
            await _tile_phase(room_id, "page_ready")
        elif meeting and current_changed and status != "done" and meeting.get("status") == "in_call":
            await _tile_phase(room_id, "drafting" if status == "working" else "")
    except Exception:  # noqa: BLE001 -- RecallError, a lost CAS race or a transport error alike
        logger.warning("meet follow-up not updated task=%s to=%s", task.get("taskId"), to, exc_info=True)


def task_link(task_id: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=task&taskId={task_id}"


async def with_link(room_id: str, task: dict, line: str) -> str:
    """A finished thread that made something (an output: a document, a page) and came from a meeting's room: its
    result line in that room carries the link, and the line goes to the Meet's chat while the bot is in the call, once
    a task (8 Nov prod Meet re-run: "put the link in chat" was never delivered: the result line had only a task card,
    and `task_moved` says a link only for `followUps`, which nothing writes since the one-Router design). Any other
    room, or a thread with no output (an answer): the line as it is."""
    if not task.get("outputs"):
        return line
    tid, linked = task["taskId"], f"{line} {task_link(task['taskId'])}"
    try:
        meeting = ((await messaging_repo.get_room(room_id)) or {}).get("meeting")
    except Exception:  # noqa: BLE001 -- the line must still post
        logger.warning("meet room %s: room not read for the result link task=%s", room_id, tid, exc_info=True)
        return line
    if not meeting:
        return line
    try:
        fresh = await meeting_invites._patch_meeting(
            room_id,
            lambda m: {"linksSaid": [*(m.get("linksSaid") or []), tid]},
            when=lambda m: (
                m.get("status") == "in_call" and m.get("recallBotId") and tid not in (m.get("linksSaid") or [])
            ),
        )  # None: not in the call, or already said (a retried Activity)
        if fresh is not None:
            await meeting_bots.say(fresh["recallBotId"], linked)
            await _tile_phase(room_id, "page_ready")
    except Exception:  # noqa: BLE001 -- a lost read, CAS race or RecallError: the room line still posts
        logger.warning("meet room %s: result link not said task=%s", room_id, tid, exc_info=True)
    return linked


class TranscriptionIn(RoomBody):
    participantId: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=200)
    optOut: bool = True


def _same(entry: dict, pid: str, name: str) -> bool:
    return bool((pid and entry.get("id") == pid) or (name and entry.get("name", "").lower() == name.lower()))


@router.post("/internal/voice/meet/{room_id}/transcription")
async def transcription(room_id: str, body: TranscriptionIn) -> dict:
    """A person asked (by voice, addressing the assistant) not to be transcribed, or to be again. Kept on the
    meeting so a worker restart or a rejoin keeps it; the Meet chat gets one line saying so (best-effort)."""
    room_id, _meta, meeting = await _meeting(room_id, body)
    pid, name = body.participantId, body.name.strip()
    if not (pid or name):
        raise _problem(400, "no_participant")

    def change(m: dict) -> dict:
        rest = [e for e in m.get("notTranscribed") or [] if not _same(e, pid, name)]
        return {"notTranscribed": [*rest, {"id": pid, "name": name}] if body.optOut else rest}

    meeting = await meeting_invites._patch_meeting(room_id, change) or meeting
    if bot := meeting.get("recallBotId"):
        who = name or "you"
        line = (f"OK {who}: your voice is excluded from the assistant and its transcript. The meeting recording continues." if body.optOut
                else f"OK {who}: you are transcribed again.")  # fmt: skip
        try:
            await meeting_bots.say(bot, line)
        except Exception as e:  # noqa: BLE001 -- the opt-out stands without the chat line
            logger.warning("meet room %s: opt-out chat failed %s", room_id, getattr(e, "code", type(e).__name__))
    return {"notTranscribed": meeting.get("notTranscribed") or []}
