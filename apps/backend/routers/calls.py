"""Voice calls with one agent. See docs/superpowers/specs/2026-09-30-voice-calls-design.md."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import voice_calls

router = APIRouter()


class StartBody(BaseModel):
    agentId: str = Field(min_length=1, max_length=128)
    agentName: str = Field(min_length=1, max_length=80)
    sdp: str = Field(min_length=1, max_length=64_000)


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
        sdp=body.sdp,
    )


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
