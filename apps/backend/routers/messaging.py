"""Canonical multiplayer HTTP commands and owner-bound agent output hook."""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import time
import uuid
from datetime import timedelta
from urllib.parse import quote
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile

from core.dynamodb import run_in_thread
from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.repositories import container_repo, messaging_repo
from core.services import messaging as service
from core.services import local_sessions
from core.services import local_session_grants
from core.services.live_events import get_live_event_publisher
from core.services.messaging import (
    AbortRunResponse,
    AgentMessageReceiptResponse,
    AgentMessageRequest,
    ApiProblem,
    CanonicalMessage,
    SetReactionRequest,
    UpdateMessageRequest,
    CreateMessageRequest,
    CreateRoomRequest,
    LiveConfig,
    MarkUnreadRequest,
    MemberPrefsRequest,
    MessagePage,
    PatchRoomRequest,
    ReadCursorRequest,
    ReadCursorResponse,
    RoomDetail,
    RoomListResponse,
    ImageAttachment,
    RunStatusResponse,
    SectionsRequest,
    SteerRunRequest,
    SteerRunResponse,
    TypingRequest,
    UpsertRoomMemberRequest,
    make_live_event,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def local_session_invocation(request: Request) -> local_session_grants.AgentInvocation:
    authorization = request.headers.get("Authorization", "")
    return local_session_grants.verify(
        authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    )


@router.get("/messaging/local-session-agent/resources", response_model=local_sessions.RoomResources)
async def agent_local_session_resources(auth: local_session_grants.AgentInvocation = Depends(local_session_invocation)):
    return await local_sessions.list_resources(auth, auth.room_id)


@router.get(
    "/messaging/local-session-agent/sessions/{session_id}/events", response_model=local_sessions.LocalSessionEventPage
)
async def agent_local_session_events(
    session_id: str,
    after_seq: Annotated[int, Query(alias="afterSeq", ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
):
    return await local_sessions.list_events(auth, auth.room_id, session_id, after_seq, limit)


@router.post(
    "/messaging/local-session-agent/sessions/{session_id}/commands",
    response_model=local_sessions.CommandReceipt,
    status_code=202,
)
async def agent_local_session_command(
    session_id: str,
    body: local_sessions.LocalSessionCommand,
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
):
    from core.services.local_session_relay import get_local_session_relay

    receipt = await local_sessions.submit_command(auth, auth.room_id, session_id, body)
    try:
        await get_local_session_relay().dispatch_session(auth.owner_id, receipt.session_id)
    except Exception:
        logger.warning("Local session dispatch deferred")
    return await local_sessions.get_command(auth, auth.room_id, receipt.session_id, body.request_id)


@router.get(
    "/messaging/local-session-agent/sessions/{session_id}/commands/{request_id}",
    response_model=local_sessions.CommandReceipt,
)
async def agent_local_session_receipt(
    session_id: str,
    request_id: str,
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
):
    return await local_sessions.get_command(auth, auth.room_id, session_id, request_id)


@router.get("/messaging/rooms/{room_id}/resources", response_model=local_sessions.RoomResources)
async def list_room_resources(room_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.list_resources(auth, room_id)


@router.get(
    "/messaging/rooms/{room_id}/local-sessions/{session_id}/events", response_model=local_sessions.LocalSessionEventPage
)
async def list_local_session_events(
    room_id: str,
    session_id: str,
    after_seq: Annotated[int, Query(alias="afterSeq", ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.list_events(auth, room_id, session_id, after_seq, limit)


@router.post(
    "/messaging/rooms/{room_id}/local-sessions/{session_id}/commands",
    response_model=local_sessions.CommandReceipt,
    status_code=202,
)
async def submit_local_session_command(
    room_id: str,
    session_id: str,
    body: local_sessions.LocalSessionCommand,
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.local_session_relay import get_local_session_relay

    receipt = await local_sessions.submit_command(auth, room_id, session_id, body)
    try:
        await get_local_session_relay().dispatch_session(resolve_owner_id(auth), receipt.session_id)
    except Exception:
        # Admission is durable. Desktop heartbeat retries only proven-undelivered work.
        logger.warning("Local session dispatch deferred")
    return await local_sessions.get_command(auth, room_id, receipt.session_id, body.request_id)


@router.get(
    "/messaging/rooms/{room_id}/local-sessions/{session_id}/commands/{request_id}",
    response_model=local_sessions.CommandReceipt,
)
async def get_local_session_command(
    room_id: str,
    session_id: str,
    request_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.get_command(auth, room_id, session_id, request_id)


_typing_last: dict[tuple[str, str], float] = {}

# Presence: who has a tab open and in use in this workspace. Clients heartbeat
# while the tab is visible and recently used; readers poll the snapshot. Kept
# in-process on purpose -- the backend runs one task, and there is no room-wide
# live channel to push it over (room events are per-room subscriptions).
# ponytail: a backend restart shows everyone offline until their next heartbeat
# (<= 45s), and a rolling deploy (two tasks for a minute or two, no shared
# state, no stickiness on this endpoint) makes the dot flicker for that window;
# move to a shared table if either ever matters.
PRESENCE_TTL_SECONDS = 90.0
_presence_last: dict[str, dict[str, float]] = {}


def _online_user_ids(owner_id: str, now: float) -> list[str]:
    seen = _presence_last.get(owner_id, {})
    for stale in [user_id for user_id, ts in seen.items() if now - ts > PRESENCE_TTL_SECONDS]:
        del seen[stale]
    return sorted(seen)


@router.post("/messaging/presence", status_code=204)
async def presence_heartbeat(auth: AuthContext = Depends(get_current_user)):
    _presence_last.setdefault(resolve_owner_id(auth), {})[auth.user_id] = time.monotonic()
    return Response(status_code=204)


@router.get("/messaging/presence")
async def presence_snapshot(auth: AuthContext = Depends(get_current_user)) -> dict[str, list[str]]:
    return {"online": _online_user_ids(resolve_owner_id(auth), time.monotonic())}


async def _require_hook_token(owner_id: str, request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing hook token")
    presented = authorization.removeprefix("Bearer ")
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    expected = (container or {}).get("hook_token") or ""
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        # Internal hook auth intentionally remains 401 at the router boundary.
        raise HTTPException(status_code=401, detail="invalid hook token")
    return container


@router.get("/messaging/live-config", response_model=LiveConfig)
async def live_config(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    key = await messaging_repo.ensure_user_stream(owner_id, auth.user_id)
    try:
        from core.services.teams_event_broker import get_broker

        broker = get_broker()
        if broker is not None:
            asyncio.create_task(broker.ensure_user(owner_id, auth.user_id))
    except Exception:
        pass
    return LiveConfig(
        http_endpoint=settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        realtime_endpoint=settings.APPSYNC_EVENTS_REALTIME_ENDPOINT,
        region=settings.APPSYNC_EVENTS_REGION or settings.AWS_REGION,
        user_channel_key=key,
    )


@router.get("/messaging/rooms", response_model=RoomListResponse)
async def list_rooms(auth: AuthContext = Depends(get_current_user)):
    return await service.list_rooms(auth)


@router.put("/messaging/me/sections", response_model=RoomListResponse)
async def put_sections(request: SectionsRequest, auth: AuthContext = Depends(get_current_user)):
    return await service.put_sections(auth, request)


@router.post("/messaging/rooms", response_model=RoomDetail, status_code=201)
async def create_room(request: CreateRoomRequest, auth: AuthContext = Depends(get_current_user)):
    return await service.create_room(auth, request)


@router.get("/messaging/rooms/{room_id}", response_model=RoomDetail)
async def get_room(room_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.get_room(auth, room_id)


@router.patch("/messaging/rooms/{room_id}", response_model=RoomDetail)
async def patch_room(
    room_id: str,
    request: PatchRoomRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.patch_room(auth, room_id, request)


@router.patch("/messaging/rooms/{room_id}/me", response_model=RoomDetail)
async def patch_room_me(
    room_id: str,
    request: MemberPrefsRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.patch_room_me(auth, room_id, request)


@router.post("/messaging/rooms/{room_id}/members", response_model=RoomDetail)
async def upsert_room_member(
    room_id: str,
    request: UpsertRoomMemberRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.upsert_member(auth, room_id, request)


@router.delete("/messaging/rooms/{room_id}/members/{kind}/{member_id}", status_code=204)
async def remove_room_member(
    room_id: str,
    kind: str,
    member_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    await service.remove_member(auth, room_id, kind, member_id)
    return Response(status_code=204)


@router.post("/messaging/rooms/{room_id}/leave", status_code=204)
async def leave_room(room_id: str, auth: AuthContext = Depends(get_current_user)):
    await service.leave_room(auth, room_id)
    return Response(status_code=204)


@router.get("/messaging/rooms/{room_id}/messages", response_model=MessagePage)
async def list_room_messages(
    room_id: str,
    after_seq: Annotated[int, Query(alias="afterSeq", ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.list_messages(auth, room_id, after_seq, limit)


@router.post("/messaging/rooms/{room_id}/messages", response_model=CanonicalMessage, status_code=201)
async def create_room_message(
    room_id: str,
    request: CreateMessageRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.create_human_message(auth, room_id, request)


@router.put("/messaging/rooms/{room_id}/messages/{message_id}/reactions", response_model=CanonicalMessage)
async def set_message_reaction(
    room_id: str, message_id: str, request: SetReactionRequest, auth: AuthContext = Depends(get_current_user)
):
    return await service.set_reaction(auth, room_id, message_id, request)


@router.patch("/messaging/rooms/{room_id}/messages/{message_id}", response_model=CanonicalMessage)
async def update_room_message(
    room_id: str, message_id: str, request: UpdateMessageRequest, auth: AuthContext = Depends(get_current_user)
):
    return await service.update_message(auth, room_id, message_id, request)


@router.put("/messaging/rooms/{room_id}/pins/{message_id}", response_model=RoomDetail)
async def pin_room_message(room_id: str, message_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.set_pin(auth, room_id, message_id, True)


@router.delete("/messaging/rooms/{room_id}/pins/{message_id}", response_model=RoomDetail)
async def unpin_room_message(room_id: str, message_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.set_pin(auth, room_id, message_id, False)


@router.post("/messaging/rooms/{room_id}/attachments", response_model=ImageAttachment, status_code=201)
async def upload_room_attachment(
    room_id: str,
    image: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await service.require_room_member(owner_id, auth.user_id, room_id)
    data = await image.read(service.IMAGE_MAX_BYTES + 1)
    try:
        # Off the event loop: this writes the image to EFS (NFS).
        return await run_in_thread(
            service.store_image_attachment,
            owner_id,
            room_id,
            image.filename or "image",
            image.content_type or "",
            data,
        )
    except ValueError as exc:
        raise service.ApiProblem("INVALID_REQUEST", str(exc)) from exc


@router.get("/messaging/rooms/{room_id}/attachments/{attachment_id}")
async def get_room_attachment(
    room_id: str,
    attachment_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    await service.require_room_member(owner_id, auth.user_id, room_id)
    attachment, data = await run_in_thread(service.load_image_attachment, owner_id, room_id, attachment_id)
    return Response(
        content=data,
        media_type=attachment.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(attachment.file_name)}",
        },
    )


@router.put("/messaging/rooms/{room_id}/read", response_model=ReadCursorResponse)
async def set_room_read_cursor(
    room_id: str,
    request: ReadCursorRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.set_read_cursor(auth, room_id, request)


@router.post("/messaging/rooms/{room_id}/unread", response_model=ReadCursorResponse)
async def mark_room_unread(
    room_id: str,
    request: MarkUnreadRequest | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.mark_room_unread(auth, room_id, request)


@router.post("/messaging/typing", status_code=204)
async def typing(request: TypingRequest, auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    room_id = str(request.room_id)
    await service.require_room_member(owner_id, auth.user_id, room_id)
    key = (auth.user_id, room_id)
    # No await below until the map is updated, so the event loop cannot
    # interleave two requests for the same key -- no lock needed.
    now = time.monotonic()
    elapsed = now - _typing_last.get(key, 0.0)
    if elapsed < 1.0:
        raise ApiProblem(
            "RATE_LIMITED",
            "Typing updates are limited to one per second.",
            retryable=True,
            retry_after=max(0.01, math.ceil((1.0 - elapsed) * 100) / 100),
        )
    # Evict entries outside the rate-limit window so this map tracks only
    # recently-active (user, room) pairs instead of growing forever.
    for stale_key in [k for k, ts in _typing_last.items() if now - ts > 1.0]:
        del _typing_last[stale_key]
    _typing_last[key] = now
    expires = service.utc_now() + timedelta(seconds=3 if request.is_typing else 0)
    event = make_live_event(
        "room.typing.changed",
        {
            "roomId": room_id,
            "userId": auth.user_id,
            "displayName": await service.human_display_name(auth.user_id),
            "expiresAt": expires.isoformat(),
        },
        room_id=room_id,
    )
    try:
        await get_live_event_publisher().publish_room(room_id, event)
    except Exception as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "Typing delivery is temporarily unavailable.",
            retryable=True,
            retry_after=1,
        ) from exc
    return Response(status_code=204)


async def _resolved_active_run(auth: AuthContext, run_id: str) -> tuple[str, dict, str]:
    owner_id = resolve_owner_id(auth)
    state = get_gateway_pool().resolve_run(owner_id, run_id)
    if state is None:
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    session_key = state["sessionKey"]
    from core.gateway.connection_pool import parse_lightbulb_session_key

    parsed = parse_lightbulb_session_key(session_key)
    if parsed is None:
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    await service.require_room_member(owner_id, auth.user_id, parsed["room_id"])
    if await messaging_repo.get_member(parsed["room_id"], "agent", parsed["agent_id"]) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The run's agent is no longer in this room.")
    return owner_id, state, "" if state.get("terminal") else session_key


@router.get("/messaging/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: str, auth: AuthContext = Depends(get_current_user)):
    """Read-only reconciliation for a run the client already knows about.

    ``room.run.frame`` is ephemeral (see the architecture spec) -- a client
    that reconnects or rotates its delivery key mid-run can miss the
    terminal ``run_ended`` frame and be left showing "Working" forever.
    Reuses the exact same ``pool.resolve_run`` truth as abort/steer, with no
    side effect, so the frontend can poll it on reconnect. A run this
    connection has no memory of at all (evicted from the terminal-id cache,
    or from a since-reconnected container link) is reported terminal rather
    than 404 -- from the client's point of view "unknown" and "ended" call
    for the same action: stop showing it as running.
    """
    try:
        _, _, session_key = await _resolved_active_run(auth, run_id)
    except ApiProblem as exc:
        if exc.error.code != "RUN_NOT_FOUND":
            raise
        return RunStatusResponse(run_id=run_id, state="terminal")
    return RunStatusResponse(run_id=run_id, state="running" if session_key else "terminal")


@router.post("/messaging/runs/{run_id}/abort", response_model=AbortRunResponse)
async def abort_run(run_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id, state, session_key = await _resolved_active_run(auth, run_id)
    if session_key == "":
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not container or not ip:
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    try:
        await get_gateway_pool().send_rpc(
            user_id=owner_id,
            req_id=str(uuid.uuid4()),
            method="chat.abort",
            params={"sessionKey": session_key, "runId": run_id},
            ip=ip,
            token=container["gateway_token"],
        )
    except RuntimeError as exc:
        if "unknown" in str(exc).lower() or "not found" in str(exc).lower():
            return AbortRunResponse(run_id=run_id, state="already-terminal")
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not abort the run.", retryable=True, retry_after=3) from exc
    return AbortRunResponse(run_id=run_id, state="aborting")


@router.post("/messaging/runs/{run_id}/steer", response_model=SteerRunResponse)
async def steer_run(
    run_id: str,
    request: SteerRunRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id, _, session_key = await _resolved_active_run(auth, run_id)
    if not session_key:
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    pool = get_gateway_pool()
    gate = await pool.gate_chat(user_id=auth.user_id, owner_id=owner_id, session_key=session_key)
    if gate.get("blocked"):
        raise ApiProblem("RUN_NOT_ACTIVE", gate.get("message", "Steering is blocked."))
    # Clear a racing idle-stop claim before runtime resolution or dispatch.
    await container_repo.touch_last_interactive(owner_id)
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not container or not ip:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is unavailable.", retryable=True, retry_after=3)
    try:
        await pool.send_rpc(
            user_id=owner_id,
            req_id=str(uuid.uuid4()),
            method="chat.send",
            params={
                "sessionKey": session_key,
                "message": request.text,
                "deliver": False,
                "idempotencyKey": str(uuid.uuid4()),
            },
            ip=ip,
            token=container["gateway_token"],
        )
    except Exception as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not steer the run.", retryable=True, retry_after=3) from exc
    return SteerRunResponse(run_id=run_id)


@router.post(
    "/internal/messaging/agent-messages/{owner_id}",
    response_model=AgentMessageReceiptResponse,
    status_code=201,
)
async def post_agent_message(owner_id: str, body: AgentMessageRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.create_agent_message(owner_id, body)


@router.get(
    "/internal/messaging/agent-messages/{owner_id}/receipts/{event_id}",
    response_model=AgentMessageReceiptResponse,
)
async def get_agent_message_receipt(owner_id: str, event_id: str, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.get_agent_receipt(owner_id, event_id)
