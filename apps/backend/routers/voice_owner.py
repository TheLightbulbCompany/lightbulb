"""Owner mode in the app (voice-agents spec §4.1b, §4.5, §5 Home): Live in a member's DM with the assistant is
the Router -- one realtime session in the huddle worker (apps/huddle-agent `owner.py`) -- and it calls these with
the worker secret and the room token. Reads are answered here in under a few seconds; anything open-ended is
`start_work`, the same Router trigger the meeting and the phone use, so its result lands in the DM as a task.

Who may act: the speaker is the LiveKit identity the server took from the Clerk-minted join token, and must be a
human member of this workspace seated in the DM (`voice_calls._voice_requester`). That member has full authority,
as when they type in the DM; anyone else is refused.
"""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AfterValidator, BaseModel, Field

from core.auth import AuthContext
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import integration_reads, memory_api, messaging, voice_approve, voice_state
from core.services.messaging import human_display_name
from core.services.voice_calls import _voice_requester
from routers.huddles import RoomBody, _Id, _problem, _require_room_token, _room_id, _Worker
from routers.internal_automations import VoiceStartIn, start_voice_work
from routers.voice_meet import _calls, assistant_name
from routers.voice_phone import _bookings, transcript_lines

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


@router.post("/internal/voice/owner/{room_id}/start")
async def start(room_id: str, body: OwnerIn) -> dict:
    """The assistant's name, the DM's agent (the tile the session speaks as) and the Router's state: this DM's
    recent messages, the tasks of this DM and of the member, and memory with the member's private scope."""
    room, member, agent = await _owner(room_id, body)
    keep = lambda t: (t.get("audience") or {}).get("roomId") == room or t.get("ownerUserId") == member  # noqa: E731
    name, context = await asyncio.gather(
        assistant_name(body.ownerId),
        voice_state.bounded(voice_state.context(body.ownerId, member, room, keep, member, room, [member])),
    )
    return {"assistantName": name, "agentId": agent, "context": context}


class TurnIn(OwnerIn):
    sessionId: Annotated[str, Field(min_length=1, max_length=200)]
    n: int = Field(ge=0, le=100000)  # the turn's place in the talk: with sessionId, its one message
    role: Literal["user", "assistant"]
    text: Annotated[str, Field(min_length=1, max_length=4000)]


@router.post("/internal/voice/owner/{room_id}/turn")
async def turn(room_id: str, body: TurnIn) -> dict:
    """One spoken line becomes a message in the DM (`via: voice`), so typed and spoken are one conversation. The
    member's line is theirs and wakes no agent (the voice already answered); the reply is the DM agent's. Keyed
    on the talk and turn: a retry is the same message."""
    room_id, member, agent = await _owner(room_id, body)
    key = f"voice-turn:{room_id}:{body.sessionId}:{body.n}"
    if body.role == "user":
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
            wake=False,
        )
        return {"messageId": str(msg.message_id)}
    if agent is None:
        raise _problem(409, "no_agent")
    receipt = await messaging.create_agent_message(
        body.ownerId,
        messaging.AgentMessageRequest(
            event_id=key,
            room_id=uuid.UUID(room_id),
            agent_id=uuid.UUID(agent),
            text=body.text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        attrs={"via": "voice"},
    )
    return {"messageId": str(receipt.message_id)}


class WorkIn(OwnerIn):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    brief: Annotated[str, Field(min_length=1, max_length=8000)]


@router.post("/internal/voice/owner/{room_id}/start-work", status_code=202)
async def start_work(room_id: str, body: WorkIn) -> dict:
    """An ask becomes a Router task delivered to the DM. The same ask (title and brief, normalised) from the same
    member on the same day is one task: the trigger id derives from it, so a retry after a failed start starts it
    and only a trigger that already exists is "already on it"."""
    room_id, member, agent = await _owner(room_id, body)
    ask = " ".join(f"{body.title}\n{body.brief}".lower().split())
    run_id = hashlib.sha256(f"{room_id}:{member}:{utc_now_iso()[:10]}:{ask}".encode()).hexdigest()[:32]
    # start_voice_work reads the requester from this row; writing it again on a retry is the same row
    await messaging_repo.put_voice_run(
        body.ownerId, run_id, {"roomId": room_id, "requesterUserId": member, "agentId": agent}
    )
    out = await start_voice_work(
        body.ownerId,
        VoiceStartIn(
            run_id=run_id,
            call_id=room_id,
            room_id=room_id,
            title=body.title,
            brief=f"Asked aloud in the app.\n\n{body.brief}",
        ),
        origin="owner_voice",  # a seated member's own ask: desktop_execution lets its task reach their Mac
    )
    created = out.pop("created", True)
    return {"started": created, **({} if created else {"already": True}), **out}


class ReadIn(OwnerIn):
    kind: Literal["memory", "calls", "calendar"]
    question: str = Field(default="", max_length=1000)
    days: Annotated[int, Field(le=90), AfterValidator(lambda d: max(d, 1))] = 7
    date: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    from_: str = Field(default="", alias="from", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    to: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


@router.post("/internal/voice/owner/{room_id}/read")
async def read(room_id: str, body: ReadIn) -> dict:
    """Memory (with the member's own private scope: only they hear it), call counts, the assistant's bookings."""
    room_id, member, _agent = await _owner(room_id, body)
    if body.kind == "memory":
        page = await memory_api.recall_page(
            body.ownerId, member, body.question or "recent decisions", budget_tokens=1500,
            audience={"kind": "room", "roomId": room_id},
        )  # fmt: skip
        return {"facts": [" ".join(r["content"].split())[:400] for r in page.get("results", [])[:6]]}
    if body.kind == "calls":
        return await _calls(body.ownerId, body.days, body.from_, body.to)
    rows = await _bookings(body.ownerId, body.date)
    return {"bookings": [{"start": b["sk"].removeprefix("VOICEBOOK#"), "service": b.get("service"),
                          "name": b.get("name")} for b in rows[:50]]}  # fmt: skip


@router.post("/internal/voice/owner/{room_id}/integrations")
async def integrations(room_id: str, body: OwnerIn) -> dict:
    await _owner(room_id, body)
    return {"connected": await integration_reads.connected(body.ownerId)}


class IntegrationIn(OwnerIn):
    provider: Annotated[str, Field(min_length=1, max_length=40)]
    query: str = Field(default="", max_length=300)


@router.post("/internal/voice/owner/{room_id}/integration-read")
async def integration_read(room_id: str, body: IntegrationIn) -> dict:
    """One bounded read of a connected integration (integration_reads, the meeting's reader)."""
    await _owner(room_id, body)
    out = await integration_reads.read(body.ownerId, body.provider, body.query, instructions=True)
    return out if isinstance(out, dict) else {"text": out}


class ApproveIn(OwnerIn):
    phrase: Literal["yes", "no", "undo"]
    hint: str = Field(default="", max_length=300)


@router.post("/internal/voice/owner/{room_id}/approve")
async def approve(room_id: str, body: ApproveIn) -> dict:
    """The member's spoken yes queues a one-tap confirm on their card (voice_approve); it never answers it."""
    room_id, member, _agent = await _owner(room_id, body)
    speaker = {"user_id": member, "name": await human_display_name(member), "where": ""}
    return await voice_approve.approve(body.ownerId, "app", speaker, body.phrase, body.hint, room_id=room_id)


class WorkRef(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    taskId: Annotated[str, Field(min_length=1, max_length=200)]


class EndIn(OwnerIn):
    sessionId: Annotated[str, Field(min_length=1, max_length=200)]  # the worker's job id: one talk, one row
    durationSec: int = Field(ge=0, le=86400)
    transcript: list[dict] = Field(default=[], max_length=2000)
    work: list[WorkRef] = Field(default=[], max_length=50)  # start_work's tasks, in the order asked


@router.post("/internal/voice/owner/{room_id}/end")
async def end(room_id: str, body: EndIn) -> dict:
    """The talk is kept as a hidden call room with `ownerMode`, a "Yours" row on Home: the transcript as phone
    calls keep it, a one-line summary and the started tasks as follow-ups. A talk with nothing said is not kept."""
    room_id, member, _agent = await _owner(room_id, body)
    lines = transcript_lines(body.transcript)
    if not lines:
        return {"kept": False}
    first = next((line["text"] for line in lines if line["who"] == "caller"), "")
    did = "; ".join(w.title for w in body.work)
    summary = " → ".join(x for x in (first and f"“{first[:120]}”", did) if x)
    call = {
        "startedAt": (datetime.now(UTC) - timedelta(seconds=body.durationSec)).isoformat(),
        "endedAt": utc_now_iso(),
        "durationSec": body.durationSec,
        "status": "ended",
        "outcome": "answered",
        "summary": summary,
        "transcript": lines,
        "followUps": [{"text": w.title, "status": "working", "taskId": w.taskId} for w in body.work],
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
