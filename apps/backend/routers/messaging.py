"""Canonical multiplayer HTTP commands and owner-bound agent output hook."""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.observability.metrics import put_metric
from core.observability.session_analytics import capture_session_event
from core.dynamodb import run_in_thread
from core.auth import (
    AuthContext,
    get_current_user,
    get_human_session,
    get_optional_user,
    resolve_owner_id,
    security_optional,
)
from core.config import settings
from core.repositories import container_repo, messaging_repo, user_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import activity, chat_gate
from core.services import messaging as service
from core.services import (
    a2a_tasks,
    hook_grants,
    hosted_mcp,
    network_login,
    link_preview,
    local_sessions,
    mac_live,
    native_session_decisions,
    openclaw_work,
    session_chat,
    session_pickup,
    session_links,
    session_resume,
    session_browser,
    session_screen,
    subagents,
    task_board,
)
from core.services import local_session_grants
from core.services import container_rpc, message_search, runtime_transport, voice_calls
from core.services.live_events import get_live_event_publisher, publish_room_activity
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.messaging import (
    AbortRunResponse,
    AgentMessageReceiptResponse,
    AgentMessageRequest,
    AgentPostRequest,
    ApiProblem,
    CanonicalMessage,
    ContractModel,
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


async def native_session_device(request: Request):
    """A paired Mac, never a Clerk user or an agent-supplied identity."""
    from core.services.machine_auth import verify_upgrade

    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    device = await verify_upgrade(token, "openclaw-macos")
    if device is None or not device.device_id:
        raise HTTPException(403, "Paired Mac required")
    return device


@router.post("/messaging/native-session-decisions", response_model=local_sessions.LocalSession)
async def native_session_decision(
    body: native_session_decisions.NativeDecisionRequest,
    device=Depends(native_session_device),
):
    """The owner answers a join or pick-up request, or pauses sharing, from their Mac."""
    return await native_session_decisions.decide(device, body)


async def computer_human(caller=Depends(network_login.computer_person)) -> AuthContext:
    """A signed-in computer's person (``lightbulb login``): the engine's workspace tools."""
    return caller[0]


@router.get("/local-sessions/device/rooms")
async def device_find_rooms(query: str = "", limit: int = 30, auth: AuthContext = Depends(computer_human)):
    return await hosted_mcp.rooms_for(auth, query, limit)


@router.get("/local-sessions/device/rooms/{room_id}/messages")
async def device_read_messages(
    room_id: str, after_seq: int | None = None, limit: int = 25, auth: AuthContext = Depends(computer_human)
):
    return await hosted_mcp.messages_for(auth, room_id, after_seq, limit)


@router.get("/local-sessions/device/receipt")
async def device_message_receipt(room_id: str, request_id: uuid.UUID, auth: AuthContext = Depends(computer_human)):
    return await hosted_mcp.receipt_for(auth, room_id, request_id)


@router.get("/local-sessions/device/tasks")
async def device_find_tasks(
    query: str = "", status: str = "", assignee: str = "", limit: int = 25, auth: AuthContext = Depends(computer_human)
):
    return await task_board.find_tasks(auth, query, status, assignee, limit)


@router.get("/local-sessions/device/tasks/{task}")
async def device_read_task(task: str, comments: int = 10, auth: AuthContext = Depends(computer_human)):
    return await task_board.read_task(auth, task, comments)


class DeviceTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    priority: str | None = None
    append_note: str | None = None


@router.patch("/local-sessions/device/tasks/{task}")
async def device_update_task(task: str, body: DeviceTaskUpdate, auth: AuthContext = Depends(computer_human)):
    return await task_board.update_task(auth, task, body.status, body.priority, body.append_note)


class DeviceBrowserPlace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    where: Literal["mac", "cloud"]
    native_id: str = Field(min_length=1, max_length=128)


@router.post("/local-sessions/device/sessions/browser-place")
async def device_use_browser(body: DeviceBrowserPlace, auth: AuthContext = Depends(computer_human)):
    """`use_browser` from the engine: the person's shared session whose native id (the engine
    resolved the caller's own Claude session or Codex thread) moves its next browser connection."""
    return await session_browser.place_by_native(resolve_owner_id(auth), auth.user_id, body.native_id, body.where)


async def person_or_computer(
    request: Request, credentials=Depends(security_optional)
) -> tuple[AuthContext, str | None]:
    """The web's Clerk person, or a signed-in computer's (``computer_person``) with its session's label."""
    if request.headers.get("X-Lightbulb-Proof"):
        return await network_login.computer_person(request)
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_current_user(credentials), None


async def local_session_invocation(request: Request) -> local_session_grants.AgentInvocation:
    authorization = request.headers.get("Authorization", "")
    return local_session_grants.verify(
        authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    )


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
    """An agent turn reads rooms as the person who asked it, with exactly that person's access (the
    hosted MCP's find_rooms/read_messages). The person is the turn grant's requester, set by the
    backend from the canonical trigger -- the human author of a typed message, the signed-in speaker
    of a Live run -- never by the agent. A Google Meet's turn reads as the member who invited the agents
    (the grant's reader, owner ruling 2026-10-13). Neither (a voice run another member steered, a
    Paperclip run, an agent's message): nothing is readable, since every room is private to its members."""
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
    return await hosted_mcp.rooms_for(read.auth, query, limit, workspace_only=read.workspace_only)


@router.get("/messaging/agent-read/messages")
async def agent_read_messages(
    room_id: str, after_seq: int | None = None, limit: int = 25, read: AgentRead = Depends(agent_reader)
):
    if read.workspace_only and not hosted_mcp.workspace_wide(
        await service.get_room(read.auth, room_id), await hosted_mcp.workspace_people(read.auth.org_id)
    ):
        raise ApiProblem("ROOM_FORBIDDEN", "In a meeting only channels the whole workspace is in can be read.")
    return await hosted_mcp.messages_for(read.auth, room_id, after_seq, limit)


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
    "search_messages": (
        lambda args: (message_search.MessageSearchRequest(**args),),
        lambda auth, room, req: message_search.search(auth, req),
    ),
    "search_sessions": (
        lambda args: (message_search.SessionSearchRequest(**args),),
        lambda auth, room, req: message_search.search_sessions(auth, req),
    ),
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


@router.get("/messaging/sessions", response_model=local_sessions.WorkspaceSessions)
async def list_workspace_sessions(
    auth: AuthContext = Depends(get_current_user), include_ended: bool = Query(False, alias="includeEnded")
):
    return await local_sessions.list_workspace_sessions(auth, include_ended=include_ended)


class SessionsRemoved(ContractModel):
    removed: int


@router.delete("/messaging/sessions", response_model=SessionsRemoved)
async def remove_ended_local_sessions(ended: bool = Query(...), auth: AuthContext = Depends(get_current_user)):
    """The caller takes every ended session of their own off the lists. Only `ended=true` exists."""
    if not ended:
        raise service.ApiProblem("INVALID_REQUEST", "Only ended sessions can be removed.")
    return SessionsRemoved(removed=await local_sessions.remove_ended_sessions(auth))


@router.delete("/messaging/sessions/{session_id}", status_code=204)
async def remove_local_session(session_id: str, auth: AuthContext = Depends(get_current_user)):
    """Owner-only: take one ended session off every list. 409 while it is live."""
    await local_sessions.remove_session(auth, session_id)


# Sessions are a workspace thing, not a room's (owner ruling 2026-09-24): any member reads,
# only the session's own human commands. Agents keep their room-granted routes above.
@router.get("/messaging/sessions/{session_id}", response_model=local_sessions.LocalSession)
async def get_local_session(session_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.get_session(auth, session_id)


class SessionOpenRequest(ContractModel):
    open_id: uuid.UUID


@router.post("/messaging/sessions/{session_id}/opened", status_code=204)
async def record_local_session_open(
    session_id: str, body: SessionOpenRequest, auth: AuthContext = Depends(get_current_user)
):
    row, _ = await local_sessions.authorize_session(auth, None, session_id)
    capture_session_event(
        row,
        "session_opened",
        str(body.open_id),
        actor_user_id=auth.user_id,
        origin="ios",
        own=auth.user_id == row["humanUserId"],
    )


class SessionViewingRequest(ContractModel):
    # The view just mounted and has no screen yet: the Mac is asked for history and a snapshot.
    fresh: bool = False


class SessionWatchers(ContractModel):
    via_link: int  # people on its "Anyone with the link" page: a count, never who
    watching: int  # workspace members with the terminal on screen, its owner aside; counted only while via_link > 0


@router.post("/messaging/sessions/{session_id}/viewing", response_model=SessionWatchers)
async def watch_local_session(
    session_id: str, body: SessionViewingRequest | None = None, auth: AuthContext = Depends(get_human_session)
):
    """A person keeps a running session's live terminal coming: every 15 s while it is on screen.
    Answers with how many people watch it."""
    return SessionWatchers.model_validate(
        await session_screen.watch(auth, session_id, mounted=bool(body and body.fresh))
    )


@router.post("/messaging/sessions/{session_id}/input", status_code=204)
async def type_into_local_session(
    session_id: str, body: session_screen.SessionInput, auth: AuthContext = Depends(get_human_session)
):
    """An admitted person types into a running session's live terminal (owner ruling 2026-10-03)."""
    await session_screen.type_into(auth, session_id, body)


class SessionBrowserInput(ContractModel):
    events: mac_live.MacInputEvents


@router.post("/messaging/sessions/{session_id}/browser/viewing", status_code=204)
async def watch_session_browser(session_id: str, auth: AuthContext = Depends(get_human_session)):
    """The Browser tab beats every 20 s while it is open: the session's browser frames keep coming."""
    await session_browser.watch(auth, session_id)


@router.post("/messaging/sessions/{session_id}/browser/input", status_code=204)
async def session_browser_input(
    session_id: str, body: SessionBrowserInput, auth: AuthContext = Depends(get_human_session)
):
    """Clicks and keys on a shared session's browser, from a person who may type into the session,
    in /browser/mac/input's shape, delivered where its Mac's socket is."""
    from routers import container

    row = await session_browser.input_target(auth, session_id)
    events = mac_live.dump_events(body.events)
    if local_sessions.browser_open(row) == "cloud":
        from core.services.browser_control import BrowserError

        try:
            await session_browser.cloud_input(row, events)
        except BrowserError as exc:
            raise HTTPException(409, detail={"code": "browser_not_casting", "message": str(exc)}) from None
        return
    await container.route_mac_input(
        row["ownerId"], auth.user_id, row["deviceId"], session_browser.run_id(row["sessionId"]), events
    )


@router.post("/messaging/sessions/{session_id}/chat", response_model=RoomDetail)
async def open_local_session_chat(session_id: str, auth: AuthContext = Depends(get_human_session)):
    """The session's chat room, beside its terminal: created on first open, the reader seated.
    People only (a Clerk session); an unshared session has no chat for anyone but its owner."""
    return await session_chat.open_chat(auth, session_id)


@router.patch("/messaging/sessions/{session_id}/display-title", response_model=local_sessions.LocalSession)
async def set_local_session_display_title(
    session_id: str,
    body: local_sessions.DisplayTitleRequest,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.set_display_title(auth, session_id, body.display_title)


class SessionSharingRequest(ContractModel):
    shared: bool


@router.put("/messaging/sessions/{session_id}/sharing", response_model=local_sessions.LocalSession)
async def set_local_session_sharing(
    session_id: str, body: SessionSharingRequest, auth: AuthContext = Depends(get_current_user)
):
    """Only the session's own human pauses or resumes sharing it."""
    session = await local_sessions.set_sharing(auth, session_id, body.shared)
    from core.services.local_session_relay import publish_resources_changed

    await publish_resources_changed(resolve_owner_id(auth), auth.user_id, session_id=session_id)
    return session


@router.put("/messaging/sessions/{session_id}/team-control", response_model=local_sessions.LocalSession)
async def set_local_session_team_control(
    session_id: str, body: local_sessions.TeamControlRequest, auth: AuthContext = Depends(get_current_user)
):
    """The owner's "Let teammates control this session" toggle; it turns itself off at session end."""
    return await local_sessions.set_team_control(auth, session_id, body.enabled)


@router.post("/messaging/sessions/{session_id}/join-requests", response_model=local_sessions.LocalSession)
async def request_local_session_join(session_id: str, auth: AuthContext = Depends(get_current_user)):
    """Refused (owner ruling 2026-10-26): teammates type only while teammates control is on."""
    return await local_sessions.request_join(auth, session_id)


@router.post("/messaging/sessions/{session_id}/join-requests/{user_id}", response_model=local_sessions.LocalSession)
async def decide_local_session_join(
    session_id: str,
    user_id: str,
    body: local_sessions.JoinDecisionRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """The owner allows or declines one person's ask. Allow lets just that person send."""
    return await local_sessions.decide_join(auth, session_id, user_id, body.decision, request_id=body.request_id)


@router.post("/messaging/sessions/{session_id}/pickup-requests", response_model=local_sessions.LocalSession)
async def ask_local_session_pickup(session_id: str, auth: AuthContext = Depends(get_current_user)):
    """A teammate asks the owner to allow pick-up; asking again while pending changes nothing."""
    return await local_sessions.request_pickup_access(auth, session_id)


@router.post("/messaging/sessions/{session_id}/pickup-requests/{user_id}", response_model=local_sessions.LocalSession)
async def decide_local_session_pickup_request(
    session_id: str,
    user_id: str,
    body: local_sessions.JoinDecisionRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """The owner allows (pick-up on for the whole session) or declines one person's request."""
    return await local_sessions.decide_pickup_request(
        auth, session_id, user_id, body.decision, request_id=body.request_id
    )


@router.delete("/messaging/sessions/{session_id}/senders/{user_id}", response_model=local_sessions.LocalSession)
async def remove_local_session_sender(session_id: str, user_id: str, auth: AuthContext = Depends(get_current_user)):
    """The owner takes one allowed person's access back."""
    return await local_sessions.remove_sender(auth, session_id, user_id)


@router.post("/messaging/sessions/{session_id}/pickups", response_model=session_pickup.PickupTicket)
async def request_local_session_pickup(
    session_id: str, body: session_pickup.PickupRequest, auth: AuthContext = Depends(get_current_user)
):
    return await session_pickup.request_pickup(auth, session_id, body.pickup_id)


@router.post("/messaging/sessions/{session_id}/pickups/{pickup_id}/outcome", status_code=204)
async def report_local_session_pickup(
    session_id: str, pickup_id: str, body: session_pickup.PickupOutcome, auth: AuthContext = Depends(get_current_user)
):
    await session_pickup.report_outcome(auth, session_id, pickup_id, body)


@router.post("/messaging/sessions/{session_id}/resume", response_model=local_sessions.LocalSession)
async def resume_local_session(
    session_id: str, body: session_resume.ResumeRequest, auth: AuthContext = Depends(get_human_session)
):
    """Resume and send: the owner, or someone who could send to it when it ended. It starts on the
    owner's Mac at that Mac's next heartbeat, within 24 hours."""
    return await session_resume.request_resume(auth, session_id, body)


@router.delete("/messaging/sessions/{session_id}/resume-allowed/{user_id}", response_model=local_sessions.LocalSession)
async def revoke_local_session_resume(session_id: str, user_id: str, auth: AuthContext = Depends(get_current_user)):
    """The owner takes one person's resume back."""
    return await session_resume.revoke_resume(auth, session_id, user_id)


def _log_link_change(request: Request, auth: AuthContext, session_id: str, action: str, audience: str | None) -> None:
    """Who changed a session's link, and from which client. The audit row has the person; no log had
    the client, so a link revoked 1.6 s after its audience changed (prod 2026-10-08, ISOA-732) could
    not be traced to the web, the desktop webview or anything else. Never the token or the URL."""
    logger.info(
        "session link %s",
        action,
        extra={
            "session_id": session_id,
            "actor_user_id": auth.user_id,
            "audience": audience,
            "user_agent": request.headers.get("user-agent", "")[:200],
            "origin": request.headers.get("origin", "")[:100],
        },
    )


@router.put("/messaging/sessions/{session_id}/link", response_model=local_sessions.SessionLink)
async def put_local_session_link(
    session_id: str, body: local_sessions.LinkRequest, request: Request, auth: AuthContext = Depends(get_current_user)
):
    """Owner-only: create the session's one link, or change who it is for."""
    link = await local_sessions.put_link(auth, session_id, body.audience)
    _log_link_change(request, auth, session_id, "put", link.audience)
    return link


@router.get("/messaging/sessions/{session_id}/link", response_model=local_sessions.SessionLink)
async def get_local_session_link(session_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.get_link(auth, session_id)


# Public: an "anyone" link opens with no sign-in. POST so the token rides in the body. These sit
# under /public/, which API Gateway throttles on its own bucket (api-stack.ts).
@router.post("/public/session-links/open", response_model=session_links.LinkPage)
async def open_session_link(body: session_links.LinkOpenRequest, auth: AuthContext | None = Depends(get_optional_user)):
    return await session_links.open_page(auth, body.token, refresh=body.refresh)


@router.post("/public/session-links/viewing", response_model=session_links.Viewing)
async def view_session_link(body: session_links.ViewingRequest):
    """An "Anyone with the link" holder watches the live terminal, view only. No sign-in."""
    return await session_links.viewing(body.token, body.lease, body.fresh, body.retake)


@router.post("/public/session-links/join-request", response_model=session_links.JoinAsk)
async def ask_to_join_session_link(
    body: session_links.LinkTokenRequest, auth: AuthContext = Depends(get_human_session)
):
    """A signed-in viewer of an "Anyone" page asks the session's owner to let them in (D1). No
    workspace needed. Under /public/ for its throttle bucket; the Clerk session is still required."""
    return await session_links.ask_to_join(auth, body.token)


class SessionPickupSwitch(ContractModel):
    enabled: bool


@router.put("/messaging/sessions/{session_id}/pickup", response_model=local_sessions.LocalSession)
async def set_local_session_pickup(
    session_id: str, body: SessionPickupSwitch, auth: AuthContext = Depends(get_current_user)
):
    """Owner-only "Let people continue this session". Off keeps the link; it only shows the session then."""
    return await session_pickup.set_pickup(auth, session_id, body.enabled)


@router.post("/session-links/copies", response_model=session_links.CopyCode, status_code=201)
async def copy_session_link(body: session_links.CopyRequest, auth: AuthContext = Depends(get_human_session)):
    """Continue: the moment a copy is made (spec §3.5). Needs the taker's own workspace."""
    return await session_links.create_copy(auth, body.token, body.challenge)


@router.post("/public/session-copies/redeem", response_model=session_links.CopyTicket | session_links.Preparing)
async def redeem_session_copy(body: session_links.RedeemRequest):
    """The CLI's poll. No sign-in: the code plus the CLI's verifier is the credential."""
    return await session_links.redeem(body.code, body.verifier)


@router.put("/messaging/sessions/{session_id}/usage-sharing", response_model=local_sessions.LocalSession)
async def set_local_session_usage_sharing(
    session_id: str, body: local_sessions.UsageSharingRequest, auth: AuthContext = Depends(get_current_user)
):
    return await local_sessions.set_usage_sharing(auth, session_id, body.enabled)


@router.get("/messaging/sessions/{session_id}/audit", response_model=local_sessions.AuditPage)
async def list_local_session_audit(session_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.list_audit(auth, session_id)


@router.get("/messaging/sessions/{session_id}/events", response_model=local_sessions.LocalSessionEventPage)
async def list_local_session_events(
    session_id: str,
    after_seq: Annotated[int, Query(alias="afterSeq", ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.list_events(auth, None, session_id, after_seq, limit)


@router.post(
    "/messaging/sessions/{session_id}/commands",
    response_model=local_sessions.CommandReceipt,
    status_code=202,
)
async def submit_local_session_command(
    session_id: str,
    body: local_sessions.LocalSessionCommand,
    replaces_request_id: str | None = Query(None, alias="replacesRequestId", min_length=1, max_length=256),
    auth: AuthContext = Depends(get_human_session),
):
    from core.services.local_session_relay import get_local_session_relay

    receipt = await local_sessions.submit_command(auth, None, session_id, body, replaces_request_id=replaces_request_id)
    try:
        await get_local_session_relay().dispatch_session(resolve_owner_id(auth), receipt.session_id)
    except Exception:
        # Admission is durable. Desktop heartbeat retries only proven-undelivered work.
        logger.warning("Local session dispatch deferred")
    return await local_sessions.get_command(auth, None, receipt.session_id, body.request_id)


@router.get(
    "/messaging/sessions/{session_id}/commands/{request_id}",
    response_model=local_sessions.CommandReceipt,
)
async def get_local_session_command(
    session_id: str,
    request_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.get_command(auth, None, session_id, request_id)


@router.post(
    "/messaging/sessions/{session_id}/commands/{request_id}/cancel",
    response_model=local_sessions.CommandReceipt,
)
async def cancel_local_session_command(
    session_id: str,
    request_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    return await local_sessions.cancel_offline_command(auth, session_id, request_id)


@router.get("/messaging/sessions/{session_id}/outbox", response_model=local_sessions.OfflineOutbox)
async def list_local_session_outbox(session_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.list_offline_outbox(auth, session_id)


_typing_last: dict[tuple[str, str], float] = {}

#: A person typing in a room is about to send, so the room's runtime is woken while they type and
#: the cold start (54-57 s) overlaps the typing instead of the wait for a reply. At most once per
#: room per window, per backend task; the single-flight wake (runtime_transport) dedupes the rest.
TYPING_PREWAKE_EVERY_S = 120.0
_typing_prewaked: dict[str, float] = {}

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


async def _require_hook_token(owner_id: str, request: Request, *, run_event_id: str | None = None) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        _count_hook_auth_failure(owner_id, request)
        raise HTTPException(status_code=401, detail="missing hook token")
    presented = authorization.removeprefix("Bearer ")
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    expected = (container or {}).get("hook_token") or ""
    if expected and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        put_metric("internal_hook.auth_accepted", dimensions={"source": "legacy"})
        return container
    if run_event_id is not None:
        try:
            await hook_grants.require_run_event(presented, owner_id, container or {}, run_event_id)
        except HTTPException:
            _count_hook_auth_failure(owner_id, request)
            raise
        put_metric("internal_hook.auth_accepted", dimensions={"source": "run_grant"})
        return container
    _count_hook_auth_failure(owner_id, request)
    # Internal hook auth intentionally remains 401 at the router boundary.
    raise HTTPException(status_code=401, detail="invalid hook token")


@router.get("/messaging/link-preview", response_model=link_preview.LinkPreview)
async def get_link_preview(
    url: Annotated[str, Query(min_length=8, max_length=2048)],
    auth: AuthContext = Depends(get_current_user),
):
    """Rich-link card metadata for a URL in a message, fetched server-side (SSRF-guarded, cached)."""
    resolve_owner_id(auth)  # workspace fence, same as every other messaging read
    preview = await link_preview.get_preview(url)
    if preview is None:
        raise HTTPException(status_code=404, detail="no preview")
    return preview


@router.get("/messaging/feed", response_model=service.FeedResponse)
async def get_feed(auth: AuthContext = Depends(get_current_user)):
    """The workspace feed (Activity): ensured on first read, the caller seated; ``{roomId, topic}``."""
    return await service.get_feed(auth)


@router.get("/messaging/live-config", response_model=LiveConfig)
async def live_config(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    key = await messaging_repo.ensure_user_stream(owner_id, auth.user_id)
    if key is None:  # removed from the workspace: a stale JWT never revives the stream
        raise ApiProblem("WORKSPACE_MEMBERSHIP_REQUIRED", "You are no longer a member of this workspace.")
    workspace_id = await messaging_repo.enroll_sessions_member(owner_id, auth.user_id, key)
    if workspace_id is None:  # removed while this request ran
        raise ApiProblem("WORKSPACE_MEMBERSHIP_REQUIRED", "You are no longer a member of this workspace.")
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


@router.get("/messaging/activity", response_model=activity.ActivityPage)
async def list_activity(
    request: Request,
    response: Response,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=activity.MAX_LIMIT)] = activity.DEFAULT_LIMIT,
    auth: AuthContext = Depends(get_current_user),
):
    """The viewer's Activity feed: mentions, DMs, thread replies, and (page 1 only)
    the Paperclip decisions and issues waiting on them."""
    return await activity.list_activity(
        auth, cursor=cursor, limit=limit, teams_ctx=lambda: activity.teams_context(auth, request, response)
    )


@router.post("/messaging/activity/seen", response_model=activity.ActivitySeenResponse)
async def mark_activity_seen(body: activity.ActivitySeenRequest, auth: AuthContext = Depends(get_current_user)):
    return await activity.mark_seen(auth, body)


@router.post("/messaging/activity/{item_id}/actions/{action_id}", response_model=activity.ActivityItem)
async def run_activity_action(
    # 320 covers the longest id: an agent ask's `agentmsg:<36-char session>:<request id up to 256>`.
    # The session stays in it because an agent's request id is unique per session, not per workspace.
    item_id: Annotated[str, Path(max_length=320)],
    action_id: Annotated[str, Path(pattern=r"^[a-z_]{1,40}$")],
    request: Request,
    response: Response,
    body: activity.ActivityActionRequest | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    """One door for every Activity action; the backend maps it to the real call."""
    return await activity.run_action(
        auth,
        item_id,
        action_id,
        body or activity.ActivityActionRequest(),
        teams_ctx=lambda: activity.teams_context(auth, request, response),
    )


@router.put("/messaging/me/sections", response_model=RoomListResponse)
async def put_sections(request: SectionsRequest, auth: AuthContext = Depends(get_current_user)):
    return await service.put_sections(auth, request)


@router.post("/messaging/rooms", response_model=RoomDetail, status_code=201)
async def create_room(request: CreateRoomRequest, caller=Depends(person_or_computer)):
    auth, label = caller
    if label is not None and request.kind != "dm":
        raise ApiProblem("ROOM_FORBIDDEN", "A computer opens direct messages only.")
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


@router.post("/messaging/search", response_model=message_search.MessageSearchResponse)
async def search_messages(body: message_search.MessageSearchRequest, auth: AuthContext = Depends(get_current_user)):
    # POST, not GET: a query string would land in the access log.
    return await message_search.search(auth, body)


@router.post("/messaging/sessions/search", response_model=message_search.SessionSearchResponse)
async def search_sessions(body: message_search.SessionSearchRequest, auth: AuthContext = Depends(get_current_user)):
    """What was said in coding sessions: prompts and replies of the sessions the caller may read."""
    return await message_search.search_sessions(auth, body)


@router.post("/messaging/search/warm", status_code=204)
async def warm_message_search(auth: AuthContext = Depends(get_current_user)) -> Response:
    await message_search.warm(auth)
    return Response(status_code=204)


@router.post("/messaging/rooms/{room_id}/messages", response_model=CanonicalMessage, status_code=201)
async def create_room_message(
    room_id: str,
    request: CreateMessageRequest,
    caller=Depends(person_or_computer),
):
    auth, label = caller
    if label is None:
        try:  # a holder's Approve/Deny press on an A2A hold card decides it and wakes nobody
            # no workspace (resolve_owner_id's 409) is never a hold card
            handled = bool(auth.org_id) and await a2a_tasks.answer_from_message(auth, resolve_owner_id(auth), request)
        except Exception:
            logger.exception("a2a card answer failed", extra={"room": room_id})
            handled = False
        return await service.create_human_message(auth, room_id, request, wake=not handled)
    # A coding session's send through its signed-in computer: authored "<label> via <person>",
    # accountable to the person, exactly as the hosted MCP's room send is. The engine names only the
    # message it replies to; its thread is that message's (as the hosted send resolves it).
    if request.reply_to_message_id and not request.thread_root_message_id:
        parent = await messaging_repo.get_message_by_id(room_id, str(request.reply_to_message_id))
        if parent is None:
            raise ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.")
        root = parent.get("threadRootMessageId") or parent["messageId"]
        request = request.model_copy(update={"thread_root_message_id": uuid.UUID(root)})
    return await service.create_human_message(
        auth,
        room_id,
        request,
        via="lightbulb_mcp",
        author_override={
            "kind": "system",
            "id": f"mcp:{auth.user_id}:engine",
            "displayName": f"{label} via {await service.human_display_name(auth.user_id)}"[:256],
        },
    )


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
):
    # Files live with the ROOM's owner, so a foreign member's upload lands where the
    # room's own members can read it (and vice versa).
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    owner_id = meta["ownerId"]
    attachment, data = await run_in_thread(service.load_attachment, owner_id, room_id, attachment_id)
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
    membership = await service.require_room_member(owner_id, auth.user_id, room_id)
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
    # Claimed before any await, like the rate limit; the wake itself runs after the typing event.
    prewake = (
        request.is_typing and now - _typing_prewaked.get(room_id, -TYPING_PREWAKE_EVERY_S) >= TYPING_PREWAKE_EVERY_S
    )
    if prewake:
        for stale_room in [r for r, ts in _typing_prewaked.items() if now - ts >= TYPING_PREWAKE_EVERY_S]:
            del _typing_prewaked[stale_room]
        _typing_prewaked[room_id] = now
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
    if prewake:
        await _prewake_for_typing(membership[0], room_id)
    return Response(status_code=204)


async def _prewake_for_typing(meta: dict, room_id: str) -> None:
    """Wake the room OWNER's runtime (a shared-channel guest types toward the host's agents) and
    restore the profile of every agent that could answer; ``observe`` agents never run. With the
    roomId the bridge also compacts that room's session if it is near the threshold, while the
    person is still typing; the typed turn stops it when it arrives (bridge _turn_arriving). Best
    effort: a failure costs only the cold start the wake would have hidden."""
    try:
        team = [
            {"id": m["id"], "name": m.get("displayName") or "Agent"}
            for m in await messaging_repo.list_members(room_id)
            if m.get("kind") == "agent" and m.get("listenMode") != "observe"
        ]
    except Exception:  # noqa: BLE001
        logger.info("typing prewake: member read failed", exc_info=True)
        return
    if team and not meta.get("archivedAt"):
        voice_calls.prewake_owner(meta.get("ownerId") or "", team, room_id)


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


async def _subagent_scope(auth: AuthContext, room_id: str, agent_id: str | None = None) -> str:
    """Membership is checked from the caller's workspace; the subagents live in the ROOM owner's
    runtime, so a guest in a shared/vendor room reaches that runtime, never their own."""
    meta, _ = await service.require_room_member(resolve_owner_id(auth), auth.user_id, room_id)
    if agent_id is not None:
        agent_id = service.validate_uuid(agent_id, label="agent ID")
        if await service.room_agent(room_id, agent_id, meta) is None:  # the feed: a roster author
            raise ApiProblem("SUBAGENT_NOT_FOUND", "That agent is not in this room.")
    return meta["ownerId"]


async def _subagent_control_gate(auth: AuthContext, owner_id: str, room_id: str, agent_id: str) -> None:
    gate = await chat_gate.gate_chat(
        user_id=auth.user_id, owner_id=owner_id, session_key=subagents.session_key(room_id, agent_id)
    )
    if gate.get("blocked"):
        raise ApiProblem("RUN_NOT_ACTIVE", gate.get("message", "This workspace cannot act on agents right now."))
    await container_repo.touch_last_interactive(owner_id)


@router.get("/messaging/rooms/{room_id}/subagents")
async def list_room_subagents(room_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id)
    return await subagents.list_for_room(owner_id, room_id)


@router.get("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/tail")
async def tail_subagent(room_id: str, agent_id: str, subagent_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    return await subagents.tail(owner_id, room_id, agent_id, subagent_id)


@router.post("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/steer")
async def steer_subagent(
    room_id: str,
    agent_id: str,
    subagent_id: str,
    request: SteerRunRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    await _subagent_control_gate(auth, owner_id, room_id, agent_id)
    return await subagents.steer(owner_id, room_id, agent_id, subagent_id, request.text)


@router.post("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/stop")
async def stop_subagent(room_id: str, agent_id: str, subagent_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    await _subagent_control_gate(auth, owner_id, room_id, agent_id)
    return await subagents.stop(owner_id, room_id, agent_id, subagent_id)


@router.post(
    "/internal/messaging/agent-messages/{owner_id}",
    response_model=AgentMessageReceiptResponse,
    status_code=201,
)
async def post_agent_message(owner_id: str, body: AgentMessageRequest, request: Request):
    await _require_hook_token(owner_id, request)
    receipt = await service.create_agent_message(owner_id, body)
    # A meeting notes agent's reply is the meeting's notes: they are mailed to its organizer.
    from core.services import meeting_notes

    await meeting_notes.on_agent_reply(
        owner_id,
        str(body.agent_id),
        str(body.reply_to_message_id) if body.reply_to_message_id else None,
        body.text,
        body.progress,
    )
    if not body.progress:
        # An A2A task waiting in this DM completes with this reply; never break the reply path.
        from core.services import a2a_tasks

        try:
            await a2a_tasks.on_agent_reply(
                owner_id,
                room_id=str(body.room_id),
                agent_id=str(body.agent_id),
                message_id=str(receipt.message_id),
                text=body.text,
                reply_to_message_id=str(body.reply_to_message_id) if body.reply_to_message_id else None,
            )
        except Exception:
            logger.exception("a2a reply link failed", extra={"owner_id": owner_id, "room_id": str(body.room_id)})
    return receipt


@router.post(
    "/internal/messaging/agent-posts/{owner_id}",
    response_model=AgentMessageReceiptResponse,
    status_code=201,
)
async def post_agent_post(owner_id: str, body: AgentPostRequest, request: Request):
    """An agent posting outside a room turn (``lightbulb-post.sh``): resolves the target
    room, then writes through the same canonical agent writer as ``agent-messages``."""
    await _require_hook_token(owner_id, request)
    return await service.post_agent_message(owner_id, body)


@router.get("/messaging/feed/u/{owner_id}")
async def agent_feed(owner_id: str, request: Request):
    """The feed's brief and latest posts for an agent outside a room turn (hook token):
    ``lightbulb-api.sh GET /api/v1/messaging/feed/u/$ISOL8_OWNER_ID``."""
    await _require_hook_token(owner_id, request)
    return await service.agent_feed(owner_id)


@router.get(
    "/internal/messaging/agent-messages/{owner_id}/receipts/{event_id}",
    response_model=AgentMessageReceiptResponse,
)
async def get_agent_message_receipt(owner_id: str, event_id: str, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.get_agent_receipt(owner_id, event_id)


@router.post("/internal/messaging/runs/{owner_id}/events", response_model=RunEventResponse)
async def post_run_event(owner_id: str, body: RunEventRequest, request: Request):
    await _require_hook_token(owner_id, request, run_event_id=str(body.run_id))
    return await service.handle_run_event(owner_id, body)


@router.post("/internal/messaging/followups/{owner_id}", response_model=FollowUpResponse, status_code=202)
async def post_follow_up(owner_id: str, body: FollowUpRequest, request: Request, response: Response):
    await _require_hook_token(owner_id, request)
    result = await service.create_follow_up(owner_id, body)
    if result.status == "replayed":
        response.status_code = 200
    return result


@router.get("/internal/messaging/rooms/{owner_id}/{room_id}/agents/{agent_id}", status_code=204)
async def get_agent_room_membership(owner_id: str, room_id: str, agent_id: str, request: Request):
    """204 when the agent is a current member of a live (not archived, not deleted) room of this
    owner, else the 404/409 problem. The bridge asks before it runs a Paperclip wake inside the
    room conversation that owns the issue, and falls back to the issue's own session on a no."""
    await _require_hook_token(owner_id, request)
    await service.require_agent_in_live_room(owner_id, room_id, agent_id)
    return Response(status_code=204)


@router.post("/internal/messaging/runtime/{owner_id}/recycle", status_code=202)
async def recycle_runtime_session(owner_id: str, request: Request):
    """The bridge asks to have its own session stopped: the 7 h recycle, ahead of AgentCore's
    8 h maxLifetime SIGTERM that kills a busy microVM mid-turn. The bridge already refuses new
    turns (a TTL'd hold), so the retried work cold-starts a fresh microVM."""
    container = await _require_hook_token(owner_id, request)
    arn = (container or {}).get("agentcore_runtime_arn")
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
    await _require_hook_token(owner_id, request)
    return await service.handle_usage_report(owner_id, body)


@router.post("/internal/messaging/subagents/{owner_id}/events")
async def post_subagent_events(owner_id: str, body: subagents.SubagentEventsRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await subagents.publish_events(owner_id, body)


class VoiceProfileRequest(ContractModel):
    agent_id: str = Field(max_length=64)
    run_id: str = Field(max_length=64)
    action: Literal["rename", "add_note", "replace_notes", "clear_notes"]
    name: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=voice_calls.NOTE_TEXT_MAX)


@router.post("/internal/agents/voice-profile/{owner_id}")
async def post_voice_profile(owner_id: str, body: VoiceProfileRequest, request: Request):
    """C8: the agent's ``lightbulb_voice_profile`` tool -- rename or voice notes, asked by voice."""
    await _require_hook_token(owner_id, request)
    return await voice_calls.edit_voice_profile(
        owner_id, agent_id=body.agent_id, run_id=body.run_id, action=body.action, name=body.name, text=body.text
    )


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
    # The run's local-session grant, minted only once the identity checked out (spec
    # 2026-10-07-agent-session-approval §3.1). It reaches only the sessions of people who let
    # background work in; an image that predates it ignores the header.
    try:
        grant = local_session_grants.mint_paperclip(
            owner_id, identity["agentId"], identity["sessionId"], identity["runId"], identity.get("issueId")
        )
    except (RuntimeError, ApiProblem):
        grant = None
        logger.warning("Paperclip local session invocation unavailable")
    return await _paperclip_hermes_runtime_response(
        container,
        "/internal/paperclip/v1/runs",
        method="POST",
        json_body={"identity": identity, "request": body},
        local_session_token=grant,
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


async def stop_paperclip_hermes_turn(owner_id: str, run: dict) -> None:
    """Best-effort stop of the Hermes turn behind Paperclip heartbeat ``run``, through the same
    runtime route the adapter's own stop uses. The bridge answers only the exact identity the run
    was launched with, and the run row does not keep it: the adapter's ``issue`` strategy named the
    session from the launch-time task, but a timer run that checks out an issue later gets that
    issue in its snapshot. So each candidate scope is tried in turn until the bridge knows one
    (404 = not this identity)."""
    snapshot = run.get("contextSnapshot") or {}
    scopes = dict.fromkeys(
        [*(f"issue:{i}" for i in (snapshot.get("taskId"), snapshot.get("issueId")) if i), f"run:{run['id']}"]
    )
    try:
        container = await container_repo.get_by_owner_id(owner_id)
        for scope in scopes:
            identity = {
                "runId": run["id"],
                "companyId": run.get("companyId"),
                "agentId": run.get("agentId"),
                "sessionId": f"paperclip:company:{run.get('companyId')}:agent:{run.get('agentId')}:{scope}",
                "paperclipApiUrl": (settings.PAPERCLIP_INTERNAL_URL or "").rstrip("/"),
                **({"issueId": scope[6:]} if scope.startswith("issue:") else {}),
            }
            response = await _paperclip_hermes_runtime_response(
                container,
                f"/internal/paperclip/v1/runs/{run['id']}/stop",
                method="POST",
                json_body={"identity": identity},
                raw=True,
            )
            if response.status_code != 404:
                return
        logger.warning("hermes turn stop: the runtime knows no identity for run=%s", run.get("id"))
    except Exception:  # noqa: BLE001 -- the Paperclip cancel already landed; a parked runtime has no turn
        logger.warning("hermes turn stop failed run=%s", run.get("id"), exc_info=True)


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
    local_session_token: str | None = None,
):
    headers = {"Authorization": f"Bearer {(container or {}).get('gateway_token') or ''}"}
    if local_session_token is not None:
        headers["X-Lightbulb-Local-Session-Token"] = local_session_token
    try:
        response = await runtime_transport.request(
            container,
            path,
            method=method,
            json_body=json_body,
            headers=headers,
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
    adapter would otherwise poll until its run timeout before giving up. A terminal
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
    body = response.json()
    if (
        isinstance(body, dict)
        and body.get("status") == "failed"
        and body.get("error") == service.PROVIDER_QUOTA_EXHAUSTED
    ):
        # Paperclip shows `error` verbatim as the run's errorMessage; give it the sentence the
        # room note uses instead of the raw code.
        detail = service.parse_quota_detail(body.pop("error_detail", None) or body.pop("errorDetail", None))
        body["error"] = service.provider_quota_message(detail)
        return body
    return Response(
        content=response.text,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )
