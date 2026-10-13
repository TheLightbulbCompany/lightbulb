"""Account-scoped Lightbulb MCP. A send is authored by the OAuth client, accountable to the person."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import messaging_repo
from core.services import clerk_admin, local_sessions, messaging, native_session_tools, session_browser, task_board

logger = logging.getLogger(__name__)

READ_SCOPE = "lightbulb:messages:read"
WRITE_SCOPE = "lightbulb:messages:write"
RESOURCE = f"{settings.PUBLIC_API_BASE.rstrip('/')}/mcp"


class ClerkOAuthVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        """Check an opaque token's revocation, audience, selected org and live membership."""
        if not settings.CLERK_SECRET_KEY or "." in token:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.clerk.com/v1/oauth_applications/access_tokens/verify",
                    headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
                    json={"access_token": token},
                )
                if response.status_code != 200:
                    return None
                verified = response.json()
                if (
                    not isinstance(verified, dict)
                    or verified.get("revoked") is not False
                    or verified.get("expired") is not False
                ):
                    return None
                subject = verified.get("subject")
                client_id = verified.get("client_id")
                scopes = verified.get("scopes")
                expiration = verified.get("expiration")
                audience = verified.get("aud")
                if (
                    not isinstance(subject, str)
                    or not subject.startswith("user_")
                    or not isinstance(client_id, str)
                    or not client_id
                    or not isinstance(audience, list)
                    or RESOURCE not in audience
                    or not isinstance(scopes, list)
                    or not all(isinstance(scope, str) for scope in scopes)
                    or not {READ_SCOPE, "user:org:read"}.issubset(scopes)
                    or not isinstance(expiration, (int, float))
                    or expiration <= time.time()
                ):
                    return None
                userinfo = await client.get(
                    f"{settings.CLERK_ISSUER}/oauth/userinfo",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if userinfo.status_code != 200:
                return None
            identity = userinfo.json()
            if not isinstance(identity, dict) or identity.get("sub") != subject:
                return None
            org_id = identity.get("org_id")
            if not isinstance(org_id, str) or not org_id.startswith("org_"):
                return None
            organizations = await clerk_admin.list_user_organizations(subject, limit=100, strict=True)
            if not any(org["id"] == org_id for org in organizations):
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(expiration),
                resource=RESOURCE,
                subject=subject,
                claims={"org_id": org_id, "iss": settings.CLERK_ISSUER},
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
            logger.warning("Lightbulb MCP OAuth verification failed")
            return None


def _caller(scope: str = READ_SCOPE) -> AuthContext:
    token = get_access_token()
    if (
        token is None
        or scope not in token.scopes
        or not token.subject
        or not token.claims
        or not isinstance(token.claims.get("org_id"), str)
    ):
        raise PermissionError(f"Lightbulb OAuth scope {scope} and selected workspace required")
    return AuthContext(
        user_id=token.subject,
        org_id=token.claims["org_id"],
        verified_issuer=token.claims.get("iss"),
    )


mcp = MCPServer(
    "Lightbulb",
    token_verifier=ClerkOAuthVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.CLERK_ISSUER),
        resource_server_url=AnyHttpUrl(RESOURCE),
        required_scopes=[READ_SCOPE, "user:org:read"],
        validate_token_resource=True,
    ),
)


# The four reads below take an AuthContext so the paired-Mac device routes
# (routers/messaging.py, /local-sessions/device/*) serve the same answers.
async def rooms_for(auth: AuthContext, query: str = "", limit: int = 30, channels_only: bool = False) -> dict:
    """`channels_only`: a Google Meet's read as its inviter. Filtered before the limit and the total, so
    neither says anything about the inviter's DMs or group chats."""
    rooms = (await messaging.list_rooms(auth)).rooms
    needle = query.casefold().strip()
    matches = [
        room
        for room in rooms
        if (not channels_only or room.kind == "channel")
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


async def sessions_for(auth: AuthContext, query: str = "", limit: int = 30) -> dict:
    sessions = (await local_sessions.list_workspace_sessions(auth)).sessions
    owner_ids = sorted({session.human_user_id for session in sessions})
    owner_names = dict(zip(owner_ids, await asyncio.gather(*(messaging.human_display_name(uid) for uid in owner_ids))))
    needle = query.casefold().strip()
    matches = [
        session
        for session in sessions
        if not needle
        or any(
            needle in value.casefold()
            for value in (
                session.display_title or "",
                session.title or "",
                session.cwd_name or "",
                session.provider,
                session.session_id,
                owner_names[session.human_user_id],
            )
        )
    ]
    return {
        "sessions": [
            {
                "sessionId": session.session_id,
                "provider": session.provider,
                "displayTitle": session.display_title,
                "title": session.title,
                "cwdName": session.cwd_name,
                "ownerUserId": session.human_user_id,
                "ownerName": owner_names[session.human_user_id],
                "status": session.status,
                "sendMode": session.capabilities.send_mode,
                # False: your own session set to Only me. Nobody else sees it, and it takes no message.
                "shared": session.shared,
                "canSend": native_session_tools.sendable(session, auth.user_id),
            }
            for session in matches[: max(1, min(limit, 50))]
        ],
        "total": len(matches),
    }


async def messages_for(auth: AuthContext, room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    count = max(1, min(limit, 50))
    if after_seq is None:
        room = await messaging.get_room(auth, room_id)
        page = await messaging.list_messages(auth, room_id, 0, count, room.last_message_seq + 1)
    else:
        page = await messaging.list_messages(auth, room_id, max(0, after_seq), count)
    return page.model_dump(mode="json", by_alias=True)


async def receipt_for(auth: AuthContext, room_id: str, request_id: uuid.UUID) -> dict:
    await messaging.require_room_member(auth.org_id, auth.user_id, room_id)
    receipt = await messaging_repo._get(
        messaging_repo.room_pk(room_id),
        messaging_repo.human_idemp_sk(auth.user_id, str(request_id)),
    )
    if receipt is None:
        return {"state": "not_found"}
    message = await messaging_repo.get_message(room_id, int(receipt["messageSeq"]))
    if message is None or message.get("via") != "lightbulb_mcp" or message.get("actorUserId") != auth.user_id:
        return {"state": "not_found"}
    return {"state": "posted", "message": messaging.canonical_message(message).model_dump(mode="json", by_alias=True)}


@mcp.tool()
async def find_rooms(query: str = "", limit: int = 30) -> dict:
    """Find channels and existing DMs that the signed-in person may read."""
    return await rooms_for(_caller(), query, limit)


@mcp.tool()
async def find_sessions(query: str = "", limit: int = 30) -> dict:
    """Find coding sessions shared with the workspace, and your own that are not (shared false: Only me, takes no message), by readable title, provider or folder. Passing a sessionId as send_message session_id types your text into that live session (canSend says whether you may; a refused send fails with a code such as LOCAL_SESSION_FORBIDDEN, LOCAL_SESSION_NOT_FOUND or LOCAL_SESSION_CONFLICT, and message_receipt then reads rejected with that reason)."""
    return await sessions_for(_caller(), query, limit)


@mcp.tool()
async def read_messages(room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    """Read the latest page or continue after a message sequence in a room or DM."""
    return await messages_for(_caller(), room_id, after_seq, limit)


async def _board(call):
    """FastMCP hides any non-ToolError text, so a refused board call says why."""
    try:
        return await call
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc


@mcp.tool()
async def find_tasks(query: str = "", status: str = "", assignee: str = "", limit: int = 25) -> dict:
    """Find tasks on this workspace's task board. Open tasks (backlog, todo, in_progress, in_review, blocked) unless status names others (comma-separated; also done, cancelled). assignee is an agent name. Newest-updated first."""
    return await _board(task_board.find_tasks(_caller(), query, status, assignee, limit))


@mcp.tool()
async def read_task(task: str, comments: int = 10) -> dict:
    """Read one task by identifier (e.g. ISOA-123) or id: title, status, priority, assignee, full description, blockers, and its latest comments."""
    return await _board(task_board.read_task(_caller(), task, comments))


@mcp.tool()
async def update_task(
    task: str, status: str | None = None, priority: str | None = None, append_note: str | None = None
) -> dict:
    """Change a task's status (backlog, todo, in_progress, in_review, blocked, done, cancelled) or priority (critical, high, medium, low), and/or append a dated note to the end of its description. This tool posts no comment: a task comment wakes the assigned agent and starts a paid run, so it is not offered here."""
    return await _board(task_board.update_task(_caller(WRITE_SCOPE), task, status, priority, append_note))


@mcp.tool()
async def use_browser(where: Literal["mac", "cloud"], session_id: str) -> dict:
    """Choose where a shared Claude Code or Codex session browses with its lightbulb-browser tools: "mac" (its Mac's Lightbulb agent Chrome, the default) or "cloud" (the workspace's cloud browser). Its next browser connection goes there; one already open stays where it is: to move an open one now, restart the lightbulb-browser MCP server (`/mcp` in Claude Code). session_id comes from find_sessions; only people who may control that session can move it."""
    auth = _caller(WRITE_SCOPE)
    try:
        sid = messaging.validate_uuid(session_id, label="session ID")
        row, _ = await local_sessions.authorize_session(auth, None, sid, owner_only=True)
        if not session_browser.live_shared(row):
            raise messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", session_browser.NOT_SHARED)
        return await session_browser.set_place(row["ownerId"], row["sessionId"], where)
    except messaging.ApiProblem as exc:
        # FastMCP hides any other exception's text, so the client would never learn why.
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc


_CLIENTS = {
    "https://claude.ai/oauth/claude-code-client-metadata": ("claude-code", "Claude Code"),
    "https://chatgpt.com/oauth/codex/client.json": ("codex", "Codex"),
}


def client_identity(client_id: str) -> tuple[str, str]:
    """(slug, display name) for the OAuth client that holds the grant."""
    if client_id in _CLIENTS:
        return _CLIENTS[client_id]
    host = urlsplit(client_id).hostname or client_id
    return host, host


def _sent_key(auth: AuthContext, request_id: uuid.UUID) -> tuple[str, str]:
    # Under the workspace's OWNER# partition, so both wipes' delete_owner_partition removes it.
    return messaging_repo.owner_pk(auth.org_id), f"MCPSEND#{auth.user_id}#{request_id}"


async def _pin_destination(auth: AuthContext, request_id: uuid.UUID, destination: str) -> dict:
    """Remember where a request_id went, so message_receipt needs only the id and a reuse elsewhere fails."""
    pk, sk = _sent_key(auth, request_id)
    # No TTL: this is the receipt index and the cross-destination guard, like the room IDEMP row.
    item = {"pk": pk, "sk": sk, "destination": destination}
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return item
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        prior = await messaging_repo._get(pk, sk)
        if prior is None or prior.get("destination") != destination:
            raise messaging.ApiProblem("IDEMPOTENCY_CONFLICT", "request_id was used for another destination.") from exc
        return prior


async def _record_outcome(
    auth: AuthContext, request_id: uuid.UUID, *, accepted: str | None = None, refusal: str | None = None
) -> None:
    """Keep a session send's outcome on its pin row, so a lost response can still be reconciled. An
    accepted outcome is final: it clears a transient refusal an earlier attempt recorded, and a later
    retry's refusal (sharing switched off since) never overwrites it."""
    pk, sk = _sent_key(auth, request_id)
    if accepted:
        # Write-once: overlapping retries must not replace a later state with an earlier snapshot.
        kwargs = {
            "UpdateExpression": "SET accepted = if_not_exists(accepted, :s) REMOVE refusal",
            "ExpressionAttributeValues": {":s": accepted},
        }
    else:
        kwargs = {
            "UpdateExpression": "SET refusal = :c",
            "ConditionExpression": "attribute_not_exists(accepted)",
            "ExpressionAttributeValues": {":c": refusal},
        }
    try:
        await run_in_thread(messaging_repo._table().update_item, Key={"pk": pk, "sk": sk}, **kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


async def _person_room(auth: AuthContext, request_id: uuid.UUID, person_id: str, *, create: bool) -> str:
    """The DM a person send goes to, pinned on its record once known: a retry or receipt keeps that
    room even after it grows into a group DM or a newer DM appears."""
    pinned = await _pin_destination(auth, request_id, f"person:{person_id}")
    if pinned.get("roomId"):
        return pinned["roomId"]
    room_id = await native_session_tools.person_dm(auth, person_id, create_key=str(request_id) if create else None)
    pk, sk = _sent_key(auth, request_id)
    updated = await run_in_thread(
        messaging_repo._table().update_item,
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET roomId = if_not_exists(roomId, :room)",
        ExpressionAttributeValues={":room": room_id},
        ReturnValues="ALL_NEW",
    )
    return updated["Attributes"]["roomId"]


async def _posted(auth: AuthContext, room_id: str, request_id: uuid.UUID) -> bool:
    """This send already committed in that room (a retry replays it wherever the room went since)."""
    return (
        await messaging_repo._get(
            messaging_repo.room_pk(room_id), messaging_repo.human_idemp_sk(auth.user_id, str(request_id))
        )
        is not None
    )


@mcp.tool()
async def send_message(
    text: str,
    request_id: uuid.UUID,
    room_id: str | None = None,
    person_id: str | None = None,
    session_id: str | None = None,
    reply_to_message_id: uuid.UUID | None = None,
    mention_ids: list[str] | None = None,
) -> dict:
    """Send as this client on the signed-in person's behalf to exactly one of room_id, person_id (a member id; opens the DM if needed) or session_id (delivers the text into that session as a follow-up prompt it acts on; it is not a link; see find_sessions canSend). Room and person sends may reply in a thread or address room members."""
    auth = _caller(WRITE_SCOPE)
    slug, client = client_identity(get_access_token().client_id)
    if sum(target is not None for target in (room_id, person_id, session_id)) != 1:
        raise messaging.ApiProblem("INVALID_REQUEST", "Send to exactly one of room_id, person_id or session_id.")
    if session_id is not None:
        if reply_to_message_id or mention_ids:
            raise messaging.ApiProblem("INVALID_REQUEST", "A session follow-up takes no thread or mentions.")
        session_id = messaging.validate_uuid(session_id, label="session ID")
        try:
            pinned = await _pin_destination(auth, request_id, f"session:{session_id}")
        except messaging.ApiProblem as exc:
            # The original destination stays authoritative, so there is no refusal to record.
            raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
        try:
            target, _ = await local_sessions.authorize_session(auth, None, session_id, owner_only=True)
            # The person's own authority (actorUserId) admits and dispatches it, exactly like their
            # follow-up from the session page; the label tells the session a client wrote it.
            result = await native_session_tools.session_send(auth, target, text, str(request_id), via_client=client)
        except messaging.ApiProblem as exc:
            if pinned.get("accepted") and exc.error.code != "IDEMPOTENCY_CONFLICT":
                # An earlier attempt admitted it and lost the response; a refusal now does not undo that.
                # (A conflict is different text under the same id: the ledger refuses it, and so do we.)
                return {"sessionId": session_id, "requestId": str(request_id), "state": pinned["accepted"]}
            # FastMCP hides any other exception's text, so the client would never learn why.
            await _record_outcome(auth, request_id, refusal=exc.error.code)
            raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
        await _record_outcome(auth, request_id, accepted=result["state"])
        return {"sessionId": session_id, "requestId": str(request_id), "state": result["state"]}
    try:
        # Checked before any room is opened, so a refused send leaves no empty DM behind.
        messaging.validate_message_text(text)
    except ValueError as exc:
        raise messaging.ApiProblem("INVALID_REQUEST", str(exc)) from exc
    if person_id is not None:
        # Reserved before the DM opens, so a conflicting reuse leaves no empty room. A reply or a
        # mention needs the DM to exist already: neither can be valid in a room opened just now.
        room_id = await _person_room(auth, request_id, person_id, create=not (reply_to_message_id or mention_ids))
    else:
        room_id = messaging.validate_uuid(room_id, label="room ID")
        await _pin_destination(auth, request_id, f"room:{room_id}")
    room = await messaging.get_room(auth, room_id)
    if person_id is not None and room.kind != "dm" and not await _posted(auth, room_id, request_id):
        # The DM became a group DM: a send meant for one person must not reach the group.
        raise messaging.ApiProblem(
            "MESSAGE_CONFLICT",
            "That direct message is now a group conversation. Send with its room_id, or with a new request_id.",
        )
    members = {member.id: member for member in room.members}
    selected = list(dict.fromkeys(mention_ids or []))
    if len(selected) > 32 or any(member_id not in members for member_id in selected):
        raise messaging.ApiProblem("MEMBER_NOT_FOUND", "Mentioned member is not in this room.")
    reply_id = str(reply_to_message_id) if reply_to_message_id else None
    thread_root_id = None
    if reply_id:
        parent = await messaging_repo.get_message_by_id(room_id, reply_id)
        if parent is None:
            raise messaging.ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.")
        thread_root_id = parent.get("threadRootMessageId") or parent["messageId"]
    message = await messaging.create_human_message(
        auth,
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=request_id,
            text=text,
            mentions=[{"kind": members[member_id].kind, "id": member_id} for member_id in selected],
            session_mentions=[],
            reply_to_message_id=uuid.UUID(reply_id) if reply_id else None,
            thread_root_message_id=uuid.UUID(thread_root_id) if thread_root_id else None,
        ),
        via="lightbulb_mcp",
        # A DM that turns into a group DM after this read refuses the write (MESSAGE_CONFLICT).
        expected_room_revision=room.room_revision if person_id is not None else None,
        author_override={
            "kind": "system",
            "id": f"mcp:{auth.user_id}:{slug}",
            "displayName": f"{client} via {await messaging.human_display_name(auth.user_id)}",
        },
    )
    return {
        "roomId": str(message.room_id),
        "messageId": str(message.message_id),
        "messageSeq": message.message_seq,
        "state": "posted",
    }


@mcp.tool()
async def message_receipt(request_id: uuid.UUID) -> dict:
    """Reconcile a send by its request ID, including when the send response was lost."""
    auth = _caller()
    pinned = await messaging_repo._get(*_sent_key(auth, request_id))
    if pinned is None:
        return {"state": "unknown"}
    kind, target = pinned["destination"].split(":", 1)
    if kind == "person":
        if not pinned.get("roomId"):
            return {"state": "not_found"}  # the send stopped before its DM was known
        kind, target = "room", pinned["roomId"]
    if kind == "room":
        return await receipt_for(auth, target, request_id)
    try:
        command = await local_sessions.get_command(auth, None, target, str(request_id))
    except messaging.ApiProblem as exc:
        # The recorded outcome answers even when get_command re-fails the same check (e.g. enrollment off):
        # an accepted send reads the state it was admitted with, a refused one reads rejected.
        if pinned.get("accepted"):
            return {"sessionId": target, "requestId": str(request_id), "state": pinned["accepted"]}
        if pinned.get("refusal"):
            return {"sessionId": target, "requestId": str(request_id), "state": "rejected", "reason": pinned["refusal"]}
        if exc.error.code != "LOCAL_SESSION_NOT_FOUND":
            raise
        return {"sessionId": target, "requestId": str(request_id), "state": "unknown"}
    return {"sessionId": target, "requestId": str(request_id), "state": command.state}


# API Gateway rewrites Host to the private ALB DNS name. The public OAuth URL is
# enforced by the audience check above; the API's existing CORS allowlist governs
# browser Origin access. The SDK still bounds POST bodies and validates JSON type.
def _build_app():
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


class _CurrentApp:
    """ASGI door onto the MCP app built by the CURRENT lifespan entry.

    The SDK binds a StreamableHTTPSessionManager into each app it builds, and that
    manager's run() may be entered once per instance. FastAPI's TestClient enters the
    lifespan once per test, so a single module-level app failed every entry after the
    first (231 unit tests + every chaos case, 2026-10-02). Each lifespan entry now
    builds its own app; outside a lifespan the door answers 503.
    """

    def __init__(self) -> None:
        self.app = None

    async def __call__(self, scope, receive, send) -> None:
        if self.app is None:
            if scope["type"] == "http":
                # This door is mounted at "/", so it also catches every path no router
                # owns. Only MCP's own paths are "not running"; anything else is simply
                # not a route, and must stay a 404 (removed-route tests assert that).
                path = scope.get("path", "")
                ours = path == "/mcp" or path.startswith(("/mcp/", "/.well-known/"))
                status, body = (503, b"hosted MCP is not running") if ours else (404, b"Not Found")
                await send(
                    {"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain")]}
                )
                await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


mount = _CurrentApp()


@asynccontextmanager
async def running():
    """Build the MCP app and run its session manager for one lifespan entry."""
    mount.app = _build_app()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        mount.app = None
