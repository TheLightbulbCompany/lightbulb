"""The Recall bot's page trades its ticket for a LiveKit seat in the meeting's room. No Clerk: the
ticket is the credential, and it holds only while the meeting's booked bot is live -- a bot whose
page reloads (Recall does, on reconnect) exchanges again; an ended meeting's ticket is dead.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import settings
from core.repositories import messaging_repo
from core.services import livekit_huddles, voice_calls

router = APIRouter()


class SessionBody(BaseModel):
    ticket: str = Field(min_length=1, max_length=2048)


@router.post("/meet-bot/session")
async def session(body: SessionBody):
    found = livekit_huddles.verify_meet_ticket(body.ticket)
    meeting = ((await messaging_repo.get_room(found[0])) or {}).get("meeting") or {} if found else {}
    if not (found and meeting.get("ticketNonce") == found[1] and meeting.get("status") in ("bot_created", "in_call")):
        raise HTTPException(status_code=401, detail="invalid ticket")
    room_id = found[0]
    return {
        "serverUrl": settings.LIVEKIT_URL,
        "roomName": livekit_huddles.meet_room_name(room_id),
        "token": livekit_huddles.mint_meet_token(room_id),
        "agents": await voice_calls.room_agents(room_id),
    }
