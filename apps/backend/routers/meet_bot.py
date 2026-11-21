"""The Recall bot's page trades its ticket for a LiveKit seat in the meeting's room. No Clerk: the
ticket is the credential, and it holds only while the meeting's booked bot is live -- a bot whose
page reloads (Recall does, on reconnect) exchanges again; an ended meeting's ticket is dead.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import settings
from core.engine import api
from core.voice import livekit_huddles, rooms

router = APIRouter()


class SessionBody(BaseModel):
    ticket: str = Field(min_length=1, max_length=2048)


@router.post("/meet-bot/session")
async def session(body: SessionBody):
    found = livekit_huddles.verify_meet_ticket(body.ticket)
    if not found:
        raise HTTPException(status_code=401, detail="invalid ticket")
    room_id, nonce = found
    conv = await api.find_conversation(room_id)
    meeting = (conv.annotations.get("meeting") if conv else None) or {}
    if meeting.get("ticketNonce") != nonce or meeting.get("status") not in ("bot_created", "in_call"):
        raise HTTPException(status_code=401, detail="invalid ticket")
    # "separate": Recall's audio socket seats each Meet person (meet_bridge), so the page's mic must stay off.
    mixed = meeting.get("audio") != "separate"
    return {
        "serverUrl": settings.LIVEKIT_URL,
        "roomName": livekit_huddles.meet_room_name(room_id),
        "token": livekit_huddles.mint_meet_token(room_id, mixed=mixed),
        "agents": await rooms.room_agents(conv.tenant_id, room_id),
        "mixedAudio": mixed,
    }
