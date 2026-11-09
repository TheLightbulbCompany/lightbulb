"""Owner mode in the app (one-brain plan, founder ruling 8 Nov 2026, option A): Live in a member's DM with the
assistant is ears and a mouth for the DM's Router (apps/huddle-agent `owner.py`). The worker calls these with the
worker secret and the room token: each spoken line becomes the member's message, exactly as a typed one (it wakes
the Router), and `/events` hands back the agent messages the room gets, which the voice says. Nothing here reads,
approves or starts work for the voice: the Router and its Workers do, and approval is the one-tap card.

Who may act: the speaker is the LiveKit identity the server took from the Clerk-minted join token, and must be a
human member of this workspace seated in the DM (`voice_calls._voice_requester`); anyone else is refused.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter
from pydantic import AwareDatetime, BaseModel, Field

from core.auth import AuthContext
from core.billing import voice_billing
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import messaging
from core.services.voice_calls import _voice_requester
from routers.huddles import RoomBody, _Id, _problem, _require_room_token, _room_id, _Worker
from routers.voice_meet import QuestionRef, agent_lines, answer_said_question, assistant_name, latest_seq
from routers.voice_phone import transcript_lines

router = APIRouter(dependencies=[_Worker])


class OwnerIn(RoomBody):
    speakerUserId: _Id


async def _owner(room_id: str, body: OwnerIn) -> tuple[str, str, str | None]:
    """(room, the member speaking, the DM's agent) or 404/403."""
    room_id = _room_id(room_id)
    _require_room_token(body.roomToken, room_id, body.ownerId)
    meta, members = await asyncio.gather(messaging_repo.get_room(room_id), messaging_repo.list_members(room_id))
    if not meta or meta.get("ownerId") != body.ownerId or meta.get("kind") != "dm":
        raise _problem(404, "room_not_found")
    member = _voice_requester(members, body.ownerId, body.speakerUserId)
    if member is None:
        raise _problem(403, "not_a_member")
    return room_id, member, next((m["id"] for m in members if m.get("kind") == "agent"), None)


def _cursor_key(room: str, member: str) -> str:
    return f"owner-mouth:{room}:{member}"


@router.post("/internal/voice/owner/{room_id}/start")
async def start(room_id: str, body: OwnerIn) -> dict:
    """The assistant's name, the DM's agent (the tile the voice speaks as) and `after`, the first `/events` cursor:
    the last message the previous session handled (spoken, or passed over outside a talk) when it was within the
    voice-run TTL, so a message posted while the worker reconnected is still said once; else the newest."""
    room, member, agent = await _owner(room_id, body)
    name, row = await asyncio.gather(
        assistant_name(body.ownerId), messaging_repo.get_voice_run(body.ownerId, _cursor_key(room, member))
    )
    after = int(row["spoken"]) if row and row.get("spoken") is not None else await latest_seq(room)
    return {"assistantName": name, "agentId": agent, "after": after}


class TurnIn(OwnerIn):
    sessionId: Annotated[str, Field(min_length=1, max_length=200)]
    n: int = Field(ge=0, le=100000)  # the line's place in the talk: with sessionId, its one message
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    answers: QuestionRef | None = None  # the Worker question the voice said last: this line answers it


@router.post("/internal/voice/owner/{room_id}/turn")
async def turn(room_id: str, body: TurnIn) -> dict:
    """One spoken line becomes the member's message in the DM (`via: voice`), exactly as a typed one: it wakes the
    Router (admission), which decides it like any other. Keyed on the talk and line: a retry is the same message."""
    room_id, member, _agent = await _owner(room_id, body)
    key = f"voice-turn:{room_id}:{body.sessionId}:{body.n}"
    # an answer to the question just said is still the DM's line, but wakes nobody: the answer is the input
    answered = await answer_said_question(body.ownerId, room_id, member, body.answers, body.text, key)
    msg = await messaging.create_human_message(
        AuthContext(user_id=member, org_id=body.ownerId),
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            text=body.text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="voice",
        **({"wake": False} if answered else {}),
    )
    out = {"messageId": str(msg.message_id)}
    return {**out, "answered": body.answers.qid} if answered else out


class EventsIn(OwnerIn):
    after: int = Field(ge=0)
    spoken: int | None = Field(default=None, ge=0)  # the last seq the voice handled: the next session starts there


@router.post("/internal/voice/owner/{room_id}/events")
async def events(room_id: str, body: EventsIn) -> dict:
    """The agent messages the DM got after message `after` (the Router's ack and replies, its Workers' answers),
    oldest first, and the next cursor. The worker polls this while a talk is live and says each one."""
    room, member, _agent = await _owner(room_id, body)
    if body.spoken is not None:
        await messaging_repo.put_voice_run(body.ownerId, _cursor_key(room, member), {"spoken": body.spoken})
    return await agent_lines(room, body.after)


class UsageSession(BaseModel):
    id: Annotated[str, Field(pattern=r"^[0-9]{1,5}$")]
    startedAt: AwareDatetime
    seconds: int = Field(ge=0, le=86400)


class EndIn(OwnerIn):
    sessionId: Annotated[str, Field(min_length=1, max_length=200)]  # the worker's job id: one talk, one row
    durationSec: int = Field(ge=0, le=86400)
    transcript: list[dict] = Field(default=[], max_length=2000)
    usageSessions: list[UsageSession] = Field(default=[], max_length=10000)


@router.post("/internal/voice/owner/{room_id}/end")
async def end(room_id: str, body: EndIn) -> dict:
    """The talk is kept as a hidden call room with `ownerMode`, a "Yours" row on Home: the transcript as phone
    calls keep it and a one-line summary (the tasks are the DM's own). A talk with nothing said is not kept."""
    room_id, member, _agent = await _owner(room_id, body)
    result = await _keep_talk(room_id, member, body)
    for usage in body.usageSessions:
        transaction_id = f"owner:{room_id}:{body.sessionId}:{usage.id}"
        await voice_billing.report_owner_minutes(
            body.ownerId, transaction_id, usage.startedAt.isoformat(), usage.seconds
        )
    return result


async def _keep_talk(room_id: str, member: str, body: EndIn) -> dict:
    """Save conversation history before independently retryable billing I/O."""
    lines = transcript_lines(body.transcript)
    if not lines:
        return {"kept": False}
    first = next((line["text"] for line in lines if line["who"] == "caller"), "")
    summary = f"“{first[:120]}”" if first else ""
    call = {
        "startedAt": (datetime.now(UTC) - timedelta(seconds=body.durationSec)).isoformat(),
        "endedAt": utc_now_iso(),
        "durationSec": body.durationSec,
        "status": "ended",
        "outcome": "answered",
        "summary": summary,
        "transcript": lines,
        "followUps": [],
        "ownerMode": True,
        "resolvedAt": None,
        "dmRoomId": room_id,
    }
    talk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-owner-talk:{room_id}:{body.sessionId}"))
    name = "You and the assistant"
    members = [{"kind": "human", "id": member, "displayName": None, "mentionHandle": None, "role": "owner",
                "listenMode": None}]  # fmt: skip
    try:
        await messaging_repo.create_room(
            owner_id=body.ownerId, room_id=talk_id, client_room_id=f"owner-talk:{room_id}:{body.sessionId}",
            creator_user_id=member, name=name, kind="call", members=members,
            req_hash=messaging_repo.request_hash(name, "call", talk_id),
            extra={"call": call, "hidden": True, "ownerUserId": member},
        )  # fmt: skip
    except messaging_repo.MessagingRepoError:
        if await messaging_repo.get_room(talk_id) is None:  # a retry keeps the row it made
            raise
    return {"kept": True, "roomId": talk_id}
