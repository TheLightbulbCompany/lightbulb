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
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AfterValidator, Field

from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import integration_reads, memory_api
from core.services.voice_calls import _voice_requester
from routers.huddles import RoomBody, _Id, _problem, _require_room_token, _room_id, _Worker
from routers.internal_automations import VoiceStartIn, start_voice_work
from routers.voice_meet import _calls, assistant_name
from routers.voice_phone import _bookings

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
    """The assistant's name, and the DM's agent (the tile the session speaks as)."""
    _room, _member, agent = await _owner(room_id, body)
    return {"assistantName": await assistant_name(body.ownerId), "agentId": agent}


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
