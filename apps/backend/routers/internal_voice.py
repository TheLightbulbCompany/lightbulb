"""Work asked aloud in a Live call becomes a task (lane A, Task 10; contracts-v2 §14.1).

The voice agent's `start_work` tool (bridge plugin) posts here with the turn's own ids. The requester and the
room come from the `VOICERUN` row the backend wrote when it dispatched the turn (the Mac tool's authority),
never from the model. One utterance = one run id = one trigger: a retried tool call is the same task.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo
from core.services import admission, super_agent_flags
from core.services.container_auth import _require_hook_token

router = APIRouter()


class VoiceStartIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)  # the voice request's run = the utterance
    call_id: str = Field(min_length=1, max_length=128)
    room_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20000)
    specialist: str | None = None


def _err(http_status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, **extra})


@router.post("/internal/automations/{owner_id}/voice-start", status_code=202)
async def voice_start(owner_id: str, body: VoiceStartIn, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    run = await messaging_repo.get_voice_run(owner_id, body.run_id)
    if run is None or run.get("roomId") != body.room_id:
        raise _err(404, "unknown_run")
    requester = run.get("requesterUserId")
    if not requester:
        raise _err(403, "no_requester")  # a guest on a call starts nothing
    meta = await messaging_repo.get_room(body.room_id) or {}
    lane = ts.router_lane_key(requester) if meta.get("kind") == "dm" else "shared"
    trigger_id = ts.trigger_id("voice", body.call_id, body.run_id)
    task_id = ts.router_task_id(trigger_id, 0, 0)
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=body.title.strip(),
        brief=body.brief.strip(),
        specialist=body.specialist,
        ack=f"On it: {body.title.strip()}",
        visibility="room",
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=owner_id,
        trigger_id=trigger_id,
        source="voice",
        admission="respond",
        lane=lane,
        actor_user_id=requester,
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "deliverRoomId": body.room_id,
            "callId": body.call_id,
            "utteranceId": body.run_id,
            "agentId": run.get("agentId"),
            "taskId": task_id,
            "start": start.model_dump(by_alias=True),
        },
    )
    await admission.enqueue_trigger(owner_id, trigger, room_id=body.room_id)
    return {"triggerId": trigger_id, "taskId": task_id}
