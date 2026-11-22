"""The agent runtime's callbacks for messaging, under `/api/v1/internal`: the bridge's run lifecycle, agent messages
and posts, follow-ups, usage, its session recycle, subagent events, the voice-profile tool and attachment reads. Every
route checks the owner's hook token. Mounted by the backend (`main.py`) and by the engine service (`engine_main.py`)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from core.dynamodb import run_in_thread
from core.engine import hooks
from core.engine.contract import VOICE_NOTE_TEXT_MAX
from core.services import messaging as service
from core.services import runtime_transport
from core.services.hook_grants import require_hook_token
from core.services.messaging import (
    AgentMessageReceiptResponse,
    AgentMessageRequest,
    AgentPostRequest,
    ContractModel,
    FollowUpRequest,
    FollowUpResponse,
    RunEventRequest,
    RunEventResponse,
    UsageReportRequest,
    UsageReportResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/internal/messaging/agent-messages/{owner_id}",
    response_model=AgentMessageReceiptResponse,
    status_code=201,
)
async def post_agent_message(owner_id: str, body: AgentMessageRequest, request: Request):
    await require_hook_token(owner_id, request)
    receipt = await service.create_agent_message(owner_id, body)
    return receipt


@router.post(
    "/internal/messaging/agent-posts/{owner_id}",
    response_model=AgentMessageReceiptResponse,
    status_code=201,
)
async def post_agent_post(owner_id: str, body: AgentPostRequest, request: Request):
    """An agent posting outside a room turn (``lightbulb-post.sh``): resolves the target
    room, then writes through the same canonical agent writer as ``agent-messages``."""
    await require_hook_token(owner_id, request)
    return await service.post_agent_message(owner_id, body)


@router.get(
    "/internal/messaging/agent-messages/{owner_id}/receipts/{event_id}",
    response_model=AgentMessageReceiptResponse,
)
async def get_agent_message_receipt(owner_id: str, event_id: str, request: Request):
    await require_hook_token(owner_id, request)
    return await service.get_agent_receipt(owner_id, event_id)


@router.post("/internal/messaging/runs/{owner_id}/events", response_model=RunEventResponse)
async def post_run_event(owner_id: str, body: RunEventRequest, request: Request):
    await require_hook_token(owner_id, request, run_event_id=str(body.run_id))
    if body.type == "run.heartbeat":  # a room turn's session is alive: keep it on its runtime at cut-over
        await run_in_thread(runtime_transport.note_alive, owner_id)
    return await service.handle_run_event(owner_id, body)


@router.post("/internal/messaging/followups/{owner_id}", response_model=FollowUpResponse, status_code=202)
async def post_follow_up(owner_id: str, body: FollowUpRequest, request: Request, response: Response):
    await require_hook_token(owner_id, request)
    result = await service.create_follow_up(owner_id, body)
    if result.status == "replayed":
        response.status_code = 200
    return result


@router.post("/internal/messaging/runtime/{owner_id}/recycle", status_code=202)
async def recycle_runtime_session(owner_id: str, request: Request):
    """The bridge asks to have its own session stopped: the 7 h recycle, ahead of AgentCore's
    8 h maxLifetime SIGTERM that kills a busy microVM mid-turn. The bridge already refuses new
    turns (a TTL'd hold), so the retried work cold-starts a fresh microVM."""
    container = await require_hook_token(owner_id, request)
    from core.dynamodb import run_in_thread
    from core.services.runtime_transport import live_arn

    arn = await run_in_thread(live_arn, container or {})  # the session asking: its own runtime at cut-over
    if not arn:
        raise HTTPException(status_code=409, detail="no runtime to recycle")
    from core.services.hermes_provisioning import stop_runtime_session

    if not await stop_runtime_session(owner_id, arn):
        # Non-2xx: the bridge releases its hold and retries on a later pass.
        raise HTTPException(status_code=502, detail="session stop unconfirmed")
    logger.info("runtime recycle: stopped the session for owner=%s", owner_id)
    return {"status": "stopped"}


@router.post("/internal/messaging/usage/{owner_id}", response_model=UsageReportResponse)
async def post_usage_report(owner_id: str, body: UsageReportRequest, request: Request):
    await require_hook_token(owner_id, request)
    return await service.handle_usage_report(owner_id, body)


@router.post("/internal/messaging/subagents/{owner_id}/events")
async def post_subagent_events(owner_id: str, request: Request):
    """No client shows subagents any more; the bridge still posts its events, so they are accepted and dropped."""
    await require_hook_token(owner_id, request)
    return {"published": 0}


class VoiceProfileRequest(ContractModel):
    agent_id: str = Field(max_length=64)
    run_id: str = Field(max_length=64)
    action: Literal["rename", "add_note", "replace_notes", "clear_notes"]
    name: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=VOICE_NOTE_TEXT_MAX)


@router.post("/internal/agents/voice-profile/{owner_id}")
async def post_voice_profile(owner_id: str, body: VoiceProfileRequest, request: Request):
    """C8: the agent's ``lightbulb_voice_profile`` tool -- rename or voice notes, asked by voice."""
    await require_hook_token(owner_id, request)
    return await hooks.tool(
        "voice_profile",
        hooks.ToolCall(
            tenant_id=owner_id,
            agent_id=body.agent_id,
            run_id=body.run_id,
            args={"action": body.action, "name": body.name, "text": body.text},
        ),
    )


@router.get("/internal/messaging/attachments/{owner_id}/{room_id}/{attachment_id}")
async def get_internal_attachment(owner_id: str, room_id: str, attachment_id: str, request: Request):
    """Hook-token twin of ``get_room_attachment`` (Clerk-authed, above) for the bridge's
    ``sync.materialize_attachments`` -- a Hermes microVM has no EFS to read attachments from
    directly, so it fetches them over this door instead (Task 25, spec §5 "Ingress")."""
    await require_hook_token(owner_id, request)
    attachment, data = await run_in_thread(service.load_attachment, owner_id, room_id, attachment_id)
    return Response(content=data, media_type=attachment.content_type)
