"""Canonical multiplayer HTTP commands and owner-bound agent output hook."""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import re
import time
from datetime import timedelta
from urllib.parse import quote
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile

from core.observability.metrics import put_metric
from core.dynamodb import run_in_thread
from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.config import settings
from core.repositories import container_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import chat_gate
from core.services import messaging as service
from core.services import local_sessions
from core.services import local_session_grants
from core.services import container_rpc, runtime_transport
from core.services.live_events import get_live_event_publisher
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.messaging import (
    AbortRunResponse,
    AgentMessageReceiptResponse,
    AgentMessageRequest,
    ApiProblem,
    CanonicalMessage,
    FollowUpRequest,
    FollowUpResponse,
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
    MessageAttachment,
    PaperclipRunRequest,
    RunEventRequest,
    RunEventResponse,
    RunStatusResponse,
    SectionsRequest,
    SteerRunRequest,
    SteerRunResponse,
    TypingRequest,
    UpsertRoomMemberRequest,
    UsageReportRequest,
    UsageReportResponse,
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
async def agent_local_session_resources(
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
    include_ended: bool = Query(False, alias="includeEnded"),
):
    return await local_sessions.list_resources(auth, auth.room_id, include_ended=include_ended)


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
async def list_room_resources(
    room_id: str,
    include_ended: bool = Query(False, alias="includeEnded"),
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.list_resources(auth, room_id, include_ended=include_ended)


@router.get("/messaging/sessions", response_model=local_sessions.WorkspaceSessions)
async def list_workspace_sessions(auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.list_workspace_sessions(auth)


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


def _count_hook_auth_failure(owner_id: str, request: Request) -> None:
    """A Paperclip agent's hermes_gateway apiKey is a SNAPSHOT of the owner's hook token, so a
    re-minted token 401s every Paperclip-woken run (nightly CEO, issue runs) while room chat keeps
    working. Counted so that silence pages; the owner goes to the log (never a metric dimension)."""
    route = "paperclip_run" if "/internal/paperclip/hermes/" in request.url.path else "other"
    logger.warning("internal hook auth failed owner=%s route=%s", owner_id, route)
    put_metric("internal_hook.auth_failed", dimensions={"route": route})


async def _require_hook_token(owner_id: str, request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        _count_hook_auth_failure(owner_id, request)
        raise HTTPException(status_code=401, detail="missing hook token")
    presented = authorization.removeprefix("Bearer ")
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    expected = (container or {}).get("hook_token") or ""
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        _count_hook_auth_failure(owner_id, request)
        # Internal hook auth intentionally remains 401 at the router boundary.
        raise HTTPException(status_code=401, detail="invalid hook token")
    return container


@router.get("/messaging/live-config", response_model=LiveConfig)
async def live_config(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    key = await messaging_repo.ensure_user_stream(owner_id, auth.user_id)
    workspace_id = await messaging_repo.ensure_workspace_id(owner_id)
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
        workspace_id=workspace_id,
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
    before_seq: Annotated[int | None, Query(alias="beforeSeq", ge=1)] = None,
    auth: AuthContext = Depends(get_current_user),
):
    return await service.list_messages(auth, room_id, after_seq, limit, before_seq)


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


@router.post("/messaging/rooms/{room_id}/attachments", response_model=MessageAttachment, status_code=201)
async def upload_room_attachment(
    room_id: str,
    image: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_user),
):
    # Files live with the ROOM's owner, so a foreign member's upload lands where the
    # room's own members can read it (and vice versa).
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]
    # Read up to the largest per-type cap (not the old flat image cap) --
    # otherwise a legitimate PDF gets silently truncated before validation
    # ever sees its true size.
    data = await image.read(service.ATTACHMENT_MAX_BYTES + 1)
    try:
        # Off the event loop: this writes the attachment to EFS (NFS).
        return await run_in_thread(
            service.store_attachment,
            owner_id,
            room_id,
            image.filename or "attachment",
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
    # Files live with the ROOM's owner, so a foreign member's upload lands where the
    # room's own members can read it (and vice versa).
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]
    attachment, data = await run_in_thread(service.load_attachment, owner_id, room_id, attachment_id)
    return Response(
        content=data,
        media_type=attachment.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            # ZIPs are opaque downloads only: never invite a browser/plugin to
            # render them inline. Images/PDF retain the existing viewer flow.
            "Content-Disposition": f"{'attachment' if attachment.content_type == 'application/zip' else 'inline'}; filename*=UTF-8''{quote(attachment.file_name, safe='')}",
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


async def _resolved_run(auth: AuthContext, run_id: str) -> tuple[str, dict]:
    owner_id = resolve_owner_id(auth)
    row = await messaging_repo.get_run(owner_id, run_id)
    if row is None:
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    await service.require_room_member(owner_id, auth.user_id, row["roomId"])
    if await messaging_repo.get_member(row["roomId"], "agent", row["agentId"]) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The run's agent is no longer in this room.")
    return owner_id, row


async def _run_control(owner_id: str, run_id: str, action: str, payload: dict) -> int | None:
    """202 accepted, 404 not active, None when the owner has no addressable runtime."""
    try:
        container = await container_rpc.running_container(owner_id)
    except ApiProblem:
        return None
    response = await runtime_transport.request(
        container,
        f"/plugins/lightbulb-channel/runs/{run_id}/{action}",
        json_body=payload,
        headers={"Authorization": f"Bearer {container['gateway_token']}"},
        timeout_s=10.0,
    )
    return response.status_code


@router.get("/messaging/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: str, auth: AuthContext = Depends(get_current_user)):
    """Read-only reconciliation for a run the client already knows about (ledger-backed)."""
    try:
        _, row = await _resolved_run(auth, run_id)
    except ApiProblem as exc:
        # A run whose agent has left the room is as over as one that never
        # existed: the client's only use for this read is to stop showing
        # "Working".
        if exc.error.code not in {"RUN_NOT_FOUND", "RUN_NOT_ACTIVE"}:
            raise
        return RunStatusResponse(run_id=run_id, state="terminal")
    return RunStatusResponse(run_id=run_id, state="terminal" if row.get("status") == "ended" else "running")


@router.post("/messaging/runs/{run_id}/abort", response_model=AbortRunResponse)
async def abort_run(run_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id, row = await _resolved_run(auth, run_id)
    if row.get("status") == "ended":
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    try:
        status = await _run_control(owner_id, run_id, "abort", {})
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not abort the run.", retryable=True, retry_after=3) from exc
    if status == 202:
        return AbortRunResponse(run_id=run_id, state="aborting")
    # Only the plugin's own "this run is not active" (404) and "no container at
    # all" (None) mean the run is over. Anything else -- a 401 from a rotated
    # gateway token, a 5xx -- is the runtime being unreachable, and reporting
    # that as terminal would tell the client the run ended while the agent
    # keeps going.
    if status in (404, None):
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not abort the run.", retryable=True, retry_after=3)


@router.post("/messaging/runs/{run_id}/steer", response_model=SteerRunResponse)
async def steer_run(run_id: str, request: SteerRunRequest, auth: AuthContext = Depends(get_current_user)):
    owner_id, row = await _resolved_run(auth, run_id)
    if row.get("status") == "ended":
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    session_key = f"agent:{row['agentId']}:lightbulb:channel:{row['roomId']}"
    gate = await chat_gate.gate_chat(user_id=auth.user_id, owner_id=owner_id, session_key=session_key)
    if gate.get("blocked"):
        raise ApiProblem("RUN_NOT_ACTIVE", gate.get("message", "Steering is blocked."))
    await container_repo.touch_last_interactive(owner_id)
    try:
        status = await _run_control(owner_id, run_id, "steer", {"text": request.text})
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not steer the run.", retryable=True, retry_after=3) from exc
    if status == 202:
        return SteerRunResponse(run_id=run_id)
    if status == 404:
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is unavailable.", retryable=True, retry_after=3)


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


@router.post("/internal/messaging/runs/{owner_id}/events", response_model=RunEventResponse)
async def post_run_event(owner_id: str, body: RunEventRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.handle_run_event(owner_id, body)


@router.post("/internal/messaging/followups/{owner_id}", response_model=FollowUpResponse, status_code=202)
async def post_follow_up(owner_id: str, body: FollowUpRequest, request: Request, response: Response):
    await _require_hook_token(owner_id, request)
    result = await service.create_follow_up(owner_id, body)
    if result.status == "replayed":
        response.status_code = 200
    return result


@router.post("/internal/messaging/usage/{owner_id}", response_model=UsageReportResponse)
async def post_usage_report(owner_id: str, body: UsageReportRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.handle_usage_report(owner_id, body)


@router.get("/internal/messaging/attachments/{owner_id}/{room_id}/{attachment_id}")
async def get_internal_attachment(owner_id: str, room_id: str, attachment_id: str, request: Request):
    """Hook-token twin of ``get_room_attachment`` (Clerk-authed, above) for the bridge's
    ``sync.materialize_attachments`` -- a Hermes microVM has no EFS to read attachments from
    directly, so it fetches them over this door instead (Task 25, spec §5 "Ingress")."""
    await _require_hook_token(owner_id, request)
    attachment, data = await run_in_thread(service.load_attachment, owner_id, room_id, attachment_id)
    return Response(content=data, media_type=attachment.content_type)


@router.post("/internal/paperclip/hermes/{owner_id}/v1/runs")
async def create_paperclip_hermes_run(owner_id: str, body: dict, request: Request):
    """Paperclip's deployed ``hermes_gateway`` adapter opens a run here.

    The whole point of this route over the old ``http`` webhook: ``202`` is an ACCEPTANCE
    RECEIPT, never a result. The adapter holds its Paperclip run open and polls
    ``GET /v1/runs/{id}`` until this facade reports a terminal status, so the Paperclip run
    ends when the HERMES TURN ends, carrying its real output. The old route's 202 was read as
    "completed", which is what produced `HTTP POST ...` issue comments and undispositioned
    issues while Hermes was still running.
    """
    container = await _require_hook_token(owner_id, request)
    identity = _paperclip_hermes_identity(request, body=body)
    await _verify_paperclip_hermes_identity(owner_id, identity, full=True)
    return await _paperclip_hermes_runtime_response(
        container,
        "/internal/paperclip/v1/runs",
        method="POST",
        json_body={"identity": identity, "request": body},
    )


@router.get("/internal/paperclip/hermes/{owner_id}/v1/runs/{run_id}")
async def get_paperclip_hermes_run(owner_id: str, run_id: str, request: Request):
    container = await _require_hook_token(owner_id, request)
    identity = _paperclip_hermes_identity(request, run_id=run_id)
    await _verify_paperclip_hermes_identity(owner_id, identity)
    response = await _paperclip_hermes_runtime_response(
        container,
        f"/internal/paperclip/v1/runs/{run_id}",
        method="GET",
        json_body={"identity": identity},
        raw=True,
    )
    return _paperclip_hermes_status_or_lost(response, run_id, identity["sessionId"])


@router.post("/internal/paperclip/hermes/{owner_id}/v1/runs/{run_id}/stop")
async def stop_paperclip_hermes_run(owner_id: str, run_id: str, request: Request):
    container = await _require_hook_token(owner_id, request)
    identity = _paperclip_hermes_identity(request, run_id=run_id)
    await _verify_paperclip_hermes_identity(owner_id, identity)
    response = await _paperclip_hermes_runtime_response(
        container,
        f"/internal/paperclip/v1/runs/{run_id}/stop",
        method="POST",
        json_body={"identity": identity},
        raw=True,
    )
    return _paperclip_hermes_status_or_lost(response, run_id, identity["sessionId"])


_HERMES_SESSION_RE = re.compile(
    r"\Apaperclip:company:([A-Za-z0-9-]+):agent:([A-Za-z0-9-]+):(issue|run):([A-Za-z0-9-]+)\Z"
)
_HERMES_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _paperclip_hermes_identity(request: Request, *, body: dict | None = None, run_id: str | None = None) -> dict:
    idempotency_key = request.headers.get("Idempotency-Key") or ""
    session_id = request.headers.get("X-Hermes-Session-Key") or ""
    if not _HERMES_RUN_ID_RE.fullmatch(idempotency_key):
        raise HTTPException(status_code=400, detail="invalid idempotency key")
    if run_id is not None and idempotency_key != run_id:
        raise HTTPException(status_code=400, detail="run id does not match idempotency key")
    match = _HERMES_SESSION_RE.fullmatch(session_id)
    if not match:
        raise HTTPException(status_code=400, detail="invalid Hermes session key")
    company_id, agent_id, scope_kind, scope_id = match.groups()
    if scope_kind == "run" and scope_id != idempotency_key:
        raise HTTPException(status_code=400, detail="run session does not match idempotency key")
    if body is not None:
        if not isinstance(body.get("input"), str) or not body["input"]:
            raise HTTPException(status_code=400, detail="input is required")
        if not isinstance(body.get("instructions"), str) or not body["instructions"]:
            raise HTTPException(status_code=400, detail="instructions are required")
        if body.get("session_id") != session_id:
            raise HTTPException(status_code=400, detail="body session does not match header")
    if not settings.PAPERCLIP_INTERNAL_URL:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "Paperclip internal URL is not configured.", retryable=True, retry_after=3
        )
    identity = {
        "runId": idempotency_key,
        "companyId": company_id,
        "agentId": agent_id,
        "sessionId": session_id,
        "paperclipApiUrl": settings.PAPERCLIP_INTERNAL_URL.rstrip("/"),
    }
    if scope_kind == "issue":
        identity["issueId"] = scope_id
    return identity


async def _verify_paperclip_hermes_identity(owner_id: str, identity: dict, *, full: bool = False) -> None:
    """The session key is attacker-shaped input, so nothing in it routes until it is checked
    against OUR canonical rows -- never the other way round.

    The company binding is the security boundary and is re-checked on EVERY call: it pins the
    session key's ``companyId`` to the Paperclip company this authenticated owner actually
    owns. It is one DynamoDB read (``resolve_owner_row`` already filters to ``status=active``).

    Agent and issue membership are checked on CREATE only (``full``), because they cost a
    Paperclip admin round-trip EACH and the adapter polls this facade throughout the run --
    verifying them per poll would put 2-3 admin calls per poll on the shared BFF for the life
    of every run. Poll and stop are safe without them: they carry no new routing decision, and
    the bridge answers them only when the FULL identity tuple matches the registry entry the
    verified create wrote (``_paperclip_identity_matches``), so a poll cannot address a run it
    did not open.
    """
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    if row is None or row.company_id != identity["companyId"]:
        raise HTTPException(status_code=403, detail="Paperclip company does not belong to owner")
    if not full:
        return
    admin = shared_admin_client()
    try:
        cookie = await get_admin_session_cookie(admin._http)
        agent = await admin.get_agent(session_cookie=cookie, agent_id=identity["agentId"])
        if (agent or {}).get("companyId") != identity["companyId"]:
            raise HTTPException(status_code=403, detail="Paperclip agent does not belong to company")
        issue_id = identity.get("issueId")
        if issue_id:
            issue = await admin.get_issue(session_cookie=cookie, issue_id=issue_id)
            if (issue or {}).get("companyId") != identity["companyId"]:
                raise HTTPException(status_code=403, detail="Paperclip issue does not belong to company")
    except HTTPException:
        raise
    except PaperclipApiError as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(status_code=403, detail="Paperclip identity is invalid") from exc
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "Could not validate Paperclip identity.", retryable=True, retry_after=3
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "Could not validate Paperclip identity.", retryable=True, retry_after=3
        ) from exc


#: Budget for a Paperclip-originated call, which arrives over the PUBLIC API Gateway — whose
#: ALB integration is hard-capped at 30 s (`api-stack.ts`'s `timeoutInMillis: 30000`). Anything
#: longer cannot be waited out: the gateway 504s over us and the caller never sees our answer.
#: `_AGENTCORE_INGRESS_TIMEOUT_S` (90 s) is right for `openclaw_work`, an SQS consumer with no
#: gateway in front, and wrong here.
#:
#: A cold microVM is 54-57 s (measured, see `runtime_transport`'s module docstring), so the
#: FIRST call to a parked workspace cannot succeed at any budget under that ceiling. Failing at
#: 25 s with a retryable 503 is the best available outcome: the adapter treats >=500 as
#: `transient_upstream` and retries onto the now-warming microVM, instead of the gateway
#: returning an uninterpretable 504 after 30 s.
PAPERCLIP_HERMES_TIMEOUT_S = 25.0


async def _paperclip_hermes_runtime_response(
    container: dict,
    path: str,
    *,
    method: str,
    json_body: dict,
    raw: bool = False,
):
    try:
        response = await runtime_transport.request(
            container,
            path,
            method=method,
            json_body=json_body,
            headers={"Authorization": f"Bearer {(container or {}).get('gateway_token') or ''}"},
            timeout_s=PAPERCLIP_HERMES_TIMEOUT_S,
        )
    # httpx.ReadTimeout is an httpx.HTTPError, so a cold-start timeout already lands here and
    # answers the same retryable 503 a transport failure does (pinned by a test). ValueError is
    # what `_request_agentcore` raises for a row that says `runtime_host=agentcore` with no
    # `agentcore_runtime_arn` — a real, if rare, state that would otherwise surface as a 500.
    except (httpx.HTTPError, ValueError) as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not reach the runtime.", retryable=True, retry_after=3) from exc
    if raw:
        return response
    return Response(
        content=response.text,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


def _paperclip_hermes_status_or_lost(response: runtime_transport.RuntimeResponse, run_id: str, session_id: str):
    """A run the microVM no longer knows about FAILS; it does not poll forever.

    The bridge's run registry is in-process and bounded, so a run vanishes exactly when the
    microVM that owned it did (AgentCore recycled the session, the process died). There is no
    honest way to report that as still-running -- the turn is not coming back -- and the
    adapter would otherwise poll until its 900 s timeout before giving up. A terminal
    ``failed`` here ends the Paperclip run promptly and says why.

    A transport failure is NOT this: it raises ``RUNTIME_UNAVAILABLE`` above (503), which the
    adapter logs and retries on its next poll, so a blip does not kill a live run.
    """
    if response.status_code == 404:
        return {
            "run_id": run_id,
            "status": "failed",
            "session_id": session_id,
            "error": "runtime_lost",
        }
    return Response(
        content=response.text,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


@router.post("/internal/paperclip/runs/{owner_id}")
async def post_paperclip_run(owner_id: str, body: PaperclipRunRequest, request: Request):
    """Legacy generic-webhook compatibility while existing agents are repointed.

    New AgentCore agents use the native ``hermes_gateway`` facade above. This old shape remains
    temporarily so an already-seeded ``http`` adapter does not go dark before the migration tail
    patches it; unlike the native route, its 202 receipt cannot represent terminal completion.
    """
    container = await _require_hook_token(owner_id, request)
    context = body.context or {}
    # Free-form record straight off Paperclip's run row -- read defensively, never assume a
    # type: a non-string here must not turn a run trigger into a 500.
    wake_reason = str(context.get("wakeReason") or "")
    task_id = str(context.get("taskId") or "")
    text = wake_reason or (f"Paperclip task {task_id}" if task_id else "Paperclip triggered a run.")
    try:
        response = await runtime_transport.request(
            container,
            f"/plugins/lightbulb-channel/inbound/{body.agent_id}",
            json_body={
                "version": 1,
                "eventId": body.run_id,
                "eventKind": "paperclip_run",
                "targetAgentId": body.agent_id,
                "sessionKey": f"agent:{body.agent_id}:paperclip:{body.run_id}",
                "message": {"text": text},
            },
            # The bridge's inbound door is gated on the GATEWAY token (server.py's
            # _authorized), never the hook token -- the hook token is for backend-side
            # routes the bridge itself calls back into (final-review C2).
            headers={"Authorization": f"Bearer {(container or {}).get('gateway_token') or ''}"},
            # AgentCore holds the invoke through a cold microVM start -- same ~90s budget as
            # the canonical ingress caller (openclaw_work._post); a Paperclip wake can hit a
            # parked workspace exactly like a room message can.
            timeout_s=_AGENTCORE_INGRESS_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not reach the runtime.", retryable=True, retry_after=3) from exc
    return Response(
        content=response.text,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )
