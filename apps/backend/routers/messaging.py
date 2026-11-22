"""Canonical multiplayer HTTP commands and owner-bound agent output hook."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import ValidationError

from core.observability.metrics import put_metric
from core.dynamodb import run_in_thread
from core.auth import (
    AuthContext,
    get_current_user,
    resolve_owner_id,
)
from core.config import settings
from core.repositories import container_repo, messaging_repo, user_repo
from core.services import chat_gate
from core.services import messaging as service
from core.services import (
    clerk_admin,
    local_sessions,
    openclaw_work,
)
from core.services import local_session_grants
from core.services import container_rpc, runtime_transport
from core.services.live_events import get_live_event_publisher, publish_room_activity
from core.services.messaging import (
    AbortRunResponse,
    ApiProblem,
    CanonicalMessage,
    ContractModel,
    CreateMessageRequest,
    CreateRoomRequest,
    LiveConfig,
    LiveFeatures,
    MarkUnreadRequest,
    MemberPrefsRequest,
    MessagePage,
    PatchRoomRequest,
    ReadCursorRequest,
    ReadCursorResponse,
    RoomDetail,
    RoomListResponse,
    MessageAttachment,
    RunStatusResponse,
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


async def workspace_people(owner_id: str) -> frozenset[str]:
    """Everyone in the workspace (Clerk). Empty when Clerk cannot say: nothing is workspace-wide then."""
    try:
        return frozenset(m["user_id"] for m in await clerk_admin.list_organization_members(owner_id, strict=True))
    except Exception:  # noqa: BLE001 -- fails closed
        logger.warning("workspace member list unavailable owner=%s", owner_id, exc_info=True)
        return frozenset()


def workspace_wide(room, people: frozenset[str]) -> bool:
    """A channel every person in the workspace is in: what any teammate could already read. Every room
    is private to its members (no channel can be browsed or joined), so this is the only public one."""
    seated = {m.id for m in room.members if m.kind == "human"}
    return room.kind == "channel" and bool(people) and people <= seated


async def rooms_for(auth: AuthContext, query: str = "", limit: int = 30, workspace_only: bool = False) -> dict:
    """`workspace_only`: a Google Meet's read as its inviter (owner ruling 2026-10-13): workspace-wide
    channels only. Filtered before the limit and the total, so neither says anything about the rest."""
    rooms = (await service.list_rooms(auth)).rooms
    people = await workspace_people(auth.org_id) if workspace_only else frozenset()
    needle = query.casefold().strip()
    matches = [
        room
        for room in rooms
        if (not workspace_only or workspace_wide(room, people))
        and (
            not needle
            or needle in (room.name or "").casefold()
            or any(needle in member.display_name.casefold() for member in room.members)
        )
    ]
    return {
        "rooms": [
            {
                "roomId": str(room.room_id),
                "name": room.name,
                "kind": room.kind,
                "members": [
                    {
                        "id": m.id,
                        "kind": m.kind,
                        "displayName": m.display_name,
                        "mentionHandle": m.mention_handle,
                    }
                    for m in room.members
                ],
            }
            for room in matches[: max(1, min(limit, 50))]
        ],
        "total": len(matches),
    }


async def messages_for(auth: AuthContext, room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    count = max(1, min(limit, 50))
    if after_seq is None:
        room = await service.get_room(auth, room_id)
        page = await service.list_messages(auth, room_id, 0, count, room.last_message_seq + 1)
    else:
        page = await service.list_messages(auth, room_id, max(0, after_seq), count)
    out = page.model_dump(mode="json", by_alias=True)
    for message in out.get("messages") or []:
        if isinstance(message.get("text"), str):
            message["text"] = messaging_repo.agent_text(message["text"])
    return out


@dataclass(frozen=True)
class AgentRead:
    """Who an agent turn reads rooms as, and whether only workspace-wide channels: a Google Meet reads
    as the member who invited the agents, and everyone on the call (outsiders included) hears it, so
    only what any teammate could read (owner ruling 2026-10-13, enforced here, not left to the agent)."""

    auth: AuthContext
    workspace_only: bool


async def agent_reader(
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
) -> AgentRead:
    """An agent turn reads rooms as the person who asked it, with exactly that person's access. The person is the turn grant's requester, set by the
    backend from the canonical trigger -- the human author of a typed message, the signed-in speaker
    of a Live run -- never by the agent. A Google Meet's turn reads as the member who invited the agents
    (the grant's reader, owner ruling 2026-10-13). Neither (a voice run another member steered,
    an agent's message): nothing is readable, since every room is private to its members."""
    person = auth.requester_id or auth.reader_id
    if not person or not auth.room_id:
        raise ApiProblem("ROOM_FORBIDDEN", "No signed-in person asked for this, so no rooms can be read for it.")
    # The turn's room, re-checked on every read as a local-session read is: the agent still a member
    # (a removed agent's unexpired grant reads nothing), and no other org in the room to hear it.
    await local_sessions.require_caller(auth, auth.room_id)
    # And the person still in it: someone who left or was removed is no longer who this turn acts for.
    if await messaging_repo.get_member(auth.room_id, "human", person) is None:
        raise ApiProblem("ROOM_FORBIDDEN", "The person who asked is no longer in this room.")
    return AgentRead(AuthContext(user_id=person, org_id=auth.owner_id), workspace_only=not auth.requester_id)


@router.get("/messaging/agent-read/rooms")
async def agent_find_rooms(query: str = "", limit: int = 30, read: AgentRead = Depends(agent_reader)):
    return await rooms_for(read.auth, query, limit, workspace_only=read.workspace_only)


@router.get("/messaging/agent-read/messages")
async def agent_read_messages(
    room_id: str, after_seq: int | None = None, limit: int = 25, read: AgentRead = Depends(agent_reader)
):
    if read.workspace_only and not workspace_wide(
        await service.get_room(read.auth, room_id), await workspace_people(read.auth.org_id)
    ):
        raise ApiProblem("ROOM_FORBIDDEN", "In a meeting only channels the whole workspace is in can be read.")
    return await messages_for(read.auth, room_id, after_seq, limit)


async def agent_actor(
    auth: local_session_grants.AgentInvocation = Depends(local_session_invocation),
) -> AuthContext:
    """An agent turn acts (adds a member, makes a room, pins...) with exactly the rights of the person
    who asked it: the grant's requester only. A Meet's reader may read for the room, never act for it."""
    if not auth.requester_id or not auth.room_id:
        raise ApiProblem(
            "ROOM_FORBIDDEN",
            "Only a person can ask for this: have someone in a room ask you, and you act with their access.",
        )
    return (await agent_reader(auth)).auth  # the same re-checks: agent and requester still in the room


# The browser's own service calls, so an agent changes the workspace exactly as its person would
# (18 Oct: a hand-written member row skipped the agent's delivery cursor and silenced its @mentions).
# Each action: (args -> the call's arguments, read up front so only a bad arg is the agent's mistake; the call,
# looked up when called).
AGENT_ACTIONS = {
    "add_member": (lambda args: (UpsertRoomMemberRequest(**args),), lambda *a: service.upsert_member(*a)),
    "set_listen_mode": (lambda args: (UpsertRoomMemberRequest(**args),), lambda *a: service.upsert_member(*a)),
    "remove_member": (lambda args: (args["kind"], args["id"]), lambda *a: service.remove_member(*a)),
    "create_room": (lambda args: (CreateRoomRequest(**args),), lambda auth, room, req: service.create_room(auth, req)),
    "update_room": (lambda args: (PatchRoomRequest(**args),), lambda *a: service.patch_room(*a)),
    "pin": (lambda args: (args["message_id"], True), lambda *a: service.set_pin(*a)),
    "unpin": (lambda args: (args["message_id"], False), lambda *a: service.set_pin(*a)),
}


class AgentActRequest(ContractModel):
    action: Literal[tuple(AGENT_ACTIONS)]  # type: ignore[valid-type]
    room_id: str | None = None
    args: dict = {}


@router.post("/messaging/agent-act")
async def agent_act(
    body: AgentActRequest,
    turn: local_session_grants.AgentInvocation = Depends(local_session_invocation),
    auth: AuthContext = Depends(agent_actor),
):
    outcome = "error"
    parse, call = AGENT_ACTIONS[body.action]
    try:
        try:
            parsed = parse(body.args)
        except ValidationError as exc:
            raise ApiProblem(
                "INVALID_REQUEST", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            ) from exc
        except KeyError as exc:
            raise ApiProblem("INVALID_REQUEST", f"args.{exc.args[0]} is required.") from exc
        result = await call(auth, body.room_id, *parsed)
        outcome = "ok"
        return {"ok": True} if result is None else result  # remove_member returns nothing
    except ApiProblem:
        outcome = "refused"
        raise
    finally:
        logger.info(
            "agent-act %s agent=%s turn=%s requester=%s room=%s outcome=%s",
            body.action,
            turn.agent_id,
            turn.turn_id,
            turn.requester_id,
            body.room_id,
            outcome,
        )
        put_metric("messaging.agent_act", dimensions={"action": body.action, "outcome": outcome})


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


_typing_last: dict[tuple[str, str], float] = {}


@router.get("/messaging/live-config", response_model=LiveConfig, response_model_exclude_none=True)
async def live_config(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    key = await messaging_repo.ensure_user_stream(owner_id, auth.user_id)
    if key is None:  # removed from the workspace: a stale JWT never revives the stream
        raise ApiProblem("WORKSPACE_MEMBERSHIP_REQUIRED", "You are no longer a member of this workspace.")
    workspace_id = await messaging_repo.enroll_sessions_member(owner_id, auth.user_id, key)
    if workspace_id is None:  # removed while this request ran
        raise ApiProblem("WORKSPACE_MEMBERSHIP_REQUIRED", "You are no longer a member of this workspace.")
    return LiveConfig(
        http_endpoint=settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        realtime_endpoint=settings.APPSYNC_EVENTS_REALTIME_ENDPOINT,
        region=settings.APPSYNC_EVENTS_REGION or settings.AWS_REGION,
        user_channel_key=key,
        workspace_id=workspace_id,
        features=LiveFeatures(super_agent=True),  # a constant since the switch retired: old app builds read it
    )


@router.get("/messaging/rooms", response_model=RoomListResponse)
async def list_rooms(auth: AuthContext = Depends(get_current_user)):
    return await service.list_rooms(auth)


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


@router.delete("/messaging/rooms/{room_id}", status_code=204)
async def delete_room(room_id: str, auth: AuthContext = Depends(get_current_user)):
    await service.delete_room(auth, room_id)
    return Response(status_code=204)


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
    if len(data) > service.ATTACHMENT_MAX_BYTES:
        # A video's own cap is larger, so a truncated read could otherwise pass as a valid file.
        raise service.ApiProblem("INVALID_REQUEST", f"attachment exceeds {service.ATTACHMENT_MAX_BYTES} bytes")
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
    head: Annotated[int | None, Query(ge=1, le=service.ATTACHMENT_HEAD_MAX)] = None,
):
    # Files live with the ROOM's owner, so a foreign member's upload lands where the
    # room's own members can read it (and vice versa).
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]
    # `?head=N`: only the first N bytes (a doc tile's preview), by a ranged read from storage.
    attachment, data = await (
        run_in_thread(service.load_attachment_head, owner_id, room_id, attachment_id, head)
        if head
        else run_in_thread(service.load_attachment, owner_id, room_id, attachment_id)
    )
    # ZIPs are opaque downloads; HTML must never render on this origin (a page an agent
    # wrote, or was tricked into writing, would run as the API). Both download; the
    # viewer renders HTML in a sandboxed frame from the fetched text instead.
    download_only = attachment.content_type in ("application/zip", "text/html")
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{'attachment' if download_only else 'inline'}; filename*=UTF-8''{quote(attachment.file_name, safe='')}",
    }
    if attachment.content_type == "text/html":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=data, media_type=attachment.content_type, headers=headers)


@router.get("/messaging/rooms/{room_id}/attachments/{attachment_id}/url")
async def get_room_attachment_url(
    room_id: str,
    attachment_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """A short-lived presigned URL for one attachment -- what a video player loads, since a
    ``<video src>`` carries no bearer and the byte route above cannot return a 100 MB file."""
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    try:
        url = await run_in_thread(service.attachment_url, meta["ownerId"], room_id, attachment_id)
    except ValueError as exc:  # a malformed id
        raise service.ApiProblem("MESSAGE_NOT_FOUND", "Attachment not found.") from exc
    return {"url": url, "expiresIn": service.ATTACHMENT_URL_TTL_S}


@router.put("/messaging/rooms/{room_id}/read", response_model=ReadCursorResponse)
async def set_room_read_cursor(
    room_id: str,
    request: ReadCursorRequest,
    auth: AuthContext = Depends(get_current_user),
):
    result = await service.set_read_cursor(auth, room_id, request)
    asyncio.create_task(user_repo.touch_active(auth.user_id))  # presence for the away email (T15)
    return result


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
    try:
        await publish_room_activity(
            room_id,
            {"kind": "human", "id": auth.user_id, "displayName": event["payload"]["displayName"]},
            "typing" if request.is_typing else "idle",
            expires if request.is_typing else None,
            skip_user_id=auth.user_id,
        )
    except Exception:
        logger.warning("room activity hint failed", exc_info=True)  # the list hint is best-effort
    return Response(status_code=204)


async def _resolved_run(auth: AuthContext, run_id: str) -> tuple[str, dict]:
    owner_id = resolve_owner_id(auth)
    row = await messaging_repo.get_run(owner_id, run_id)
    if row is None:
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    await service.require_room_member(owner_id, auth.user_id, row["roomId"])
    if await service.room_agent(row["roomId"], row["agentId"]) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The run's agent is no longer in this room.")
    return owner_id, row


async def _run_control(owner_id: str, run_id: str, action: str, payload: dict, grant: str | None = None) -> int | None:
    """202 accepted, 404 not active, None when the owner has no addressable runtime. `grant`: a
    steer's lowered local-session grant (openclaw_work.steer_grant)."""
    try:
        container = await container_rpc.running_container(owner_id)
    except ApiProblem:
        return None
    response = await runtime_transport.request(
        container,
        f"/plugins/lightbulb-channel/runs/{run_id}/{action}",
        json_body=payload,
        headers={
            "Authorization": f"Bearer {container['gateway_token']}",
            **({"X-Lightbulb-Local-Session-Token": grant} if grant else {}),
        },
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
    # Only the plugin's own "this run is not active" (404) and "no container at
    # all" (None) mean the run is over. Anything else -- a 401 from a rotated
    # gateway token, a 5xx -- is the runtime being unreachable, and reporting
    # that as terminal would tell the client the run ended while the agent
    # keeps going.
    if status == 202:
        # Accepted is not stopped (prod 2026-09-25: the ledger said aborted while the agent
        # ran on for 1m45s). The run closes on the bridge's own run.ended(aborted), which
        # follows the interrupt within seconds; the client shows "Stopping…" until then. If it
        # never comes, the backend closes the run itself after the grace -- this timer, and the
        # sweeper (request_abort pins the deadline) if this task dies first.
        await messaging_repo.request_abort(
            owner_id, run_id, stopped_by=auth.user_id, stop_after_s=service.seconds_since(row.get("startedAt"))
        )
        _force_close_later(owner_id, run_id)
        return AbortRunResponse(run_id=run_id, state="aborting")
    if status in (404, None):
        await service.close_aborted_run(owner_id, run_id)
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not abort the run.", retryable=True, retry_after=3)


_abort_timers: set[asyncio.Task] = set()


def _force_close_later(owner_id: str, run_id: str) -> None:
    async def _close() -> None:
        await asyncio.sleep(messaging_repo.RUN_ABORT_GRACE_S)
        try:
            await service.close_aborted_run(owner_id, run_id, forced=True)  # no-op once ended
        except Exception:
            logger.exception("forced abort close failed run=%s", run_id)

    task = asyncio.create_task(_close())
    _abort_timers.add(task)
    task.add_done_callback(_abort_timers.discard)


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
        # Another member steering this run must not borrow its requester's access.
        grant = await openclaw_work.steer_grant(owner_id, row, auth.user_id)
    except (RuntimeError, ApiProblem, KeyError) as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not steer the run.", retryable=True, retry_after=3) from exc
    try:
        status = await _run_control(owner_id, run_id, "steer", {"text": request.text}, grant)
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not steer the run.", retryable=True, retry_after=3) from exc
    if status == 202:
        return SteerRunResponse(run_id=run_id)
    if status == 404:
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is unavailable.", retryable=True, retry_after=3)
