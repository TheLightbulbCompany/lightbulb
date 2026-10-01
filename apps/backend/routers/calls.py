"""Voice calls: a DM call with one agent, or a room call with the team. See docs/superpowers/specs/2026-10-01-voice-v2-design.md."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import voice_calls

router = APIRouter()


class StartBody(BaseModel):
    agentId: str | None = Field(default=None, min_length=1, max_length=128)
    agentName: str | None = Field(default=None, min_length=1, max_length=80)
    roomId: str | None = Field(default=None, min_length=1, max_length=128)
    sdp: str = Field(min_length=1, max_length=64_000)

    @model_validator(mode="after")
    def _one_target(self):
        if (self.agentId is None) == (self.roomId is None):
            raise ValueError("exactly one of agentId / roomId")
        if self.agentId and not self.agentName:
            raise ValueError("agentName is required with agentId")
        return self


class TurnBody(BaseModel):
    seq: int = Field(ge=1)  # client-monotonic: the newest utterance wins, not the first claim to land
    utterance: str = Field(min_length=1, max_length=4_000)
    transcript: str = Field(default="", max_length=12_000)


@router.post("")
async def start(body: StartBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.start_call(
        owner_id=resolve_owner_id(auth),
        user_id=auth.user_id,
        agent_id=body.agentId,
        agent_name=body.agentName,
        room_id=body.roomId,
        sdp=body.sdp,
    )


@router.get("/active")
async def active(
    roomId: Annotated[str, Query(min_length=1, max_length=128)], auth: Annotated[AuthContext, Depends(get_current_user)]
):
    return {"calls": await voice_calls.active_callers(resolve_owner_id(auth), auth.user_id, roomId)}


@router.post("/{call_id}/turns")
async def turn(call_id: str, body: TurnBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.start_turn(
        call_id=call_id,
        owner_id=resolve_owner_id(auth),
        user_id=auth.user_id,
        seq=body.seq,
        utterance=body.utterance,
        transcript=body.transcript,
    )


@router.post("/{call_id}/end")
async def end(call_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.end_call(call_id=call_id, owner_id=resolve_owner_id(auth), user_id=auth.user_id)
