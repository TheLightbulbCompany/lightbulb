"""Account-scoped Lightbulb MCP. A send is authored by the OAuth client, accountable to the person."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

import httpx
import jwt
from botocore.exceptions import ClientError
from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import local_sessions_repo, messaging_repo
from core.services import (
    agent_keys,
    clerk_admin,
    container_rpc,
    local_sessions,
    messaging,
    native_session_tools,
    network_member,
    session_browser,
    task_board,
    webhook_agents,
)
from core.services.machine_auth import MachineIdentity, verify_upgrade

logger = logging.getLogger(__name__)

# The same hints the Mac's own list carried: Codex and Claude Code read them to decide
# which calls ask first. Only send_message is destructive.
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

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
                claims={"org_id": org_id, "iss": settings.CLERK_ISSUER, **await _registered_client(client_id)},
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
            logger.warning("Lightbulb MCP OAuth verification failed")
            return None


class LightbulbVerifier(ClerkOAuthVerifier):
    """Two callers share one tool set: a person (a Clerk opaque grant) or a shared session on a
    paired Mac (its device JWT, whose signed ``source`` claim names the calling session)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(agent_keys.PREFIX):
            return await self._verify_agent_key(token)
        if "." not in token:
            return await super().verify_token(token)
        device = await verify_upgrade(token, "openclaw-macos")
        if device is None or not device.device_id or not device.org_id:
            return None
        try:
            await local_sessions._enrolled(device.owner_id, device.user_id, device.device_id)
        except messaging.ApiProblem:
            return None
        claims = jwt.decode(token, options={"verify_signature": False})  # verify_upgrade checked it
        return AccessToken(
            token=token,
            client_id=f"device:{device.device_id}",
            scopes=[READ_SCOPE, WRITE_SCOPE, "user:org:read"],
            expires_at=int(claims["exp"]),
            resource=RESOURCE,
            subject=device.user_id,
            claims={"org_id": device.org_id, "device": asdict(device), "source": claims.get("source")},
        )

    async def _verify_agent_key(self, token: str) -> AccessToken | None:
        """A per-agent key acts as its person, only while they are still in the workspace."""
        try:
            row = await agent_keys.resolve(token)
            if row is None:
                return None
            organizations = await clerk_admin.list_user_organizations(row["userId"], limit=100, strict=True)
            if not any(org["id"] == row["ownerId"] for org in organizations):
                return None
        except (ClientError, RuntimeError, ValueError, KeyError):
            logger.warning("Lightbulb MCP agent key verification failed")  # never the key text
            return None
        logger.info("agent key call keyId=%s owner=%s", row["keyId"], row["ownerId"])
        return AccessToken(
            token=token,
            client_id=f"agentkey:{row['keyId']}",
            scopes=[READ_SCOPE, WRITE_SCOPE, "user:org:read"],
            expires_at=int(row["expiresAt"] or time.time() + 300),
            resource=RESOURCE,
            subject=row["userId"],
            claims={
                "org_id": row["ownerId"],
                "iss": settings.CLERK_ISSUER,
                "agentKey": {"keyId": row["keyId"], "name": row["name"]},
            },
        )


def _mac() -> MachineIdentity | None:
    """The paired Mac when a shared session is calling; None for a person's OAuth grant."""
    token = get_access_token()
    device = (getattr(token, "claims", None) or {}).get("device")
    return MachineIdentity(**device) if device else None


async def _from_session(device: MachineIdentity, tool: str, arguments: dict) -> dict:
    """A session's send or receipt: the Mac-native path, which proves the source is this Mac's
    current session and keeps its own ledger (``NATIVE_TOOL#``)."""
    source = get_access_token().claims.get("source")
    try:
        if not isinstance(source, dict):
            raise messaging.ApiProblem("INVALID_REQUEST", "A Mac call names its session.")
        request = native_session_tools.NativeToolRequest.model_validate(
            {**source, "tool": tool, "arguments": arguments}
        )
        return await native_session_tools.execute(device, request)
    except messaging.ApiProblem as exc:
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
    except ValueError as exc:  # pydantic: a malformed source claim
        raise ToolError("INVALID_REQUEST: invalid session source.") from exc


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


async def unread_note(owner: str, session_id: str) -> str | None:
    """Agent network §4: the sends waiting for this session that it has not been given yet, so an
    agent that cannot be woken (Cursor, Grok CLI) notices its mail at its next lightbulb call."""
    now = int(time.time())
    count = sum(
        1
        for c in await local_sessions_repo.list_commands(owner, session_id)
        if c.get("kind") == "send"
        and c.get("state") in ("pending", "dispatched")
        and not c.get("heldFor")
        and int(c.get("deliverBy") or 0) > now
    )
    if not count:
        return None
    return f"{count} unread message{'' if count == 1 else 's'} for this session; it arrives at your next step."


async def network_unread_note() -> str | None:
    """The engine's unread line for this client's app session on the network (a hosted client
    is pull-only). Only while the workspace is warm: asking a parked one would start it."""
    from core.auth import resolve_owner_id

    auth = _caller()
    owner_id = resolve_owner_id(auth)
    if not await network_member._session_is_warm(owner_id):
        return None
    slug, _ = _sender(get_access_token())
    params = {"op": "unread", "app": slug, "person": network_member.app_person(auth.user_id)}
    # An app session not declared yet (or a runtime off the network) refuses: no note.
    result = await container_rpc.call(owner_id, "network.app", params, timeout_s=5.0)
    count = int((result or {}).get("unread") or 0) if isinstance(result, dict) else 0
    if not count:
        return None
    return f"{count} unread message{'' if count == 1 else 's'} for this session; call check_inbox to read them."


def _noted(tool):
    """Every tool result carries ``unread`` while mail waits: a shared session's undelivered
    sends, or a hosted client's network mail. The note never fails a tool."""

    @functools.wraps(tool)
    async def call(*args, **kwargs):
        result = await tool(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        device = _mac()
        source = (get_access_token().claims.get("source") or {}) if device else {}
        try:
            if device:
                note = None
                if isinstance(source.get("sourceSessionId"), str):
                    note = await unread_note(device.owner_id, source["sourceSessionId"])
            else:
                note = await network_unread_note()
            if note:
                result = {**result, "unread": note}
        except Exception:  # noqa: BLE001 -- a note never fails the tool it rides on
            logger.warning("unread note unavailable", exc_info=True)
        return result

    return call


mcp = MCPServer(
    "Lightbulb",
    token_verifier=LightbulbVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.CLERK_ISSUER),
        resource_server_url=AnyHttpUrl(RESOURCE),
        required_scopes=[READ_SCOPE, "user:org:read"],
        validate_token_resource=True,
    ),
)


# The four reads below take an AuthContext so the paired-Mac device routes
# (routers/messaging.py, /local-sessions/device/*) serve the same answers.
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
    rooms = (await messaging.list_rooms(auth)).rooms
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
                # True: the send waits until the session's owner allows you (awaiting_approval).
                "needsOwnerOk": native_session_tools.needs_owner_ok(session, auth.user_id),
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


@mcp.tool(annotations=READ)
@_noted
async def find_rooms(query: str = "", limit: int = 30) -> dict:
    """Find channels and existing DMs that the signed-in person may read."""
    return await rooms_for(_caller(), query, limit)


@mcp.tool(annotations=READ)
@_noted
async def find_sessions(query: str = "", limit: int = 30) -> dict:
    """The coding sessions you can message. Sessions on the team's lightbulb network have a session_id (an address such as "ab12cd34/claude-5"; this client's own is is_this_session): pass it as send_message session_id. Also finds coding sessions shared with the workspace, and your own that are not (shared false: Only me, takes no message), by readable title, provider or folder (these have a sessionId). Also lists agents: webhook agents such as "Aden's Grok Bot" that live in another cloud; pass an agentId as send_message agent_id (with needsOwnerOk true it waits for that agent's person to allow you). Passing a sessionId as send_message session_id types your text into that live session (canSend says whether you may; with needsOwnerOk true the send waits for the session's owner to allow you, and its state reads awaiting_approval until they do; a refused send fails with a code such as LOCAL_SESSION_FORBIDDEN, LOCAL_SESSION_NOT_FOUND or LOCAL_SESSION_CONFLICT, and message_receipt then reads rejected with that reason). Called from a shared session, that session is marked isThisSession: never message it."""
    auth = _caller()
    result = await sessions_for(auth, query, limit)
    needle = query.casefold().strip()
    result["agents"] = [
        a
        for a in await webhook_agents.targets(auth.org_id, auth.user_id)
        if not needle or any(needle in a[k].casefold() for k in ("displayName", "kind", "ownerName"))
    ][: max(1, min(limit, 50))]
    if _mac():
        own = (get_access_token().claims.get("source") or {}).get("sourceSessionId")
        for session in result["sessions"]:
            session["isThisSession"] = session["sessionId"] == own
        return result
    try:
        found = await _network_sessions(auth, query)
    except ToolError as exc:
        if "NETWORK_NOT_CONNECTED" not in str(exc):
            result["network"] = str(exc)  # e.g. starting: the network sessions come on a retry
        return result
    except Exception:  # noqa: BLE001 -- the network half never fails the rest of the list
        logger.warning("network sessions unavailable", exc_info=True)
        return result
    result["sessions"] = [*found, *result["sessions"]][: max(1, min(limit, 50))]
    result["total"] += len(found)
    return result


async def _network_sessions(auth: AuthContext, query: str) -> list[dict]:
    """The member's ``directory``, in the engine's ``find_sessions`` shape."""
    slug, _ = _sender(get_access_token())
    own = f"/{slug}-{network_member.app_person(auth.user_id)}"
    answer = await _through_member(auth, "directory", query=query)
    found = answer.get("sessions") if isinstance(answer, dict) else None
    out = []
    for row in found if isinstance(found, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("session_id"), str):
            continue
        row = dict(row)
        if handle := row.pop("handle", None):
            row["name"] = handle
        row["is_this_session"] = row["session_id"].endswith(own)
        out.append(row)
    return out


@mcp.tool(annotations=READ)
@_noted
async def read_messages(room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    """Read the latest page or continue after a message sequence in a room or DM."""
    return await messages_for(_caller(), room_id, after_seq, limit)


async def _board(call):
    """FastMCP hides any non-ToolError text, so a refused board call says why."""
    try:
        return await call
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc


@mcp.tool(annotations=READ)
@_noted
async def find_tasks(query: str = "", status: str = "", assignee: str = "", limit: int = 25) -> dict:
    """Find tasks on this workspace's task board. Open tasks (backlog, todo, in_progress, in_review, blocked) unless status names others (comma-separated; also done, cancelled). assignee is an agent name. Newest-updated first."""
    return await _board(task_board.find_tasks(_caller(), query, status, assignee, limit))


@mcp.tool(annotations=READ)
@_noted
async def read_task(task: str, comments: int = 10) -> dict:
    """Read one task by identifier (e.g. ISOA-123) or id: title, status, priority, assignee, full description, blockers, and its latest comments."""
    return await _board(task_board.read_task(_caller(), task, comments))


@mcp.tool(annotations=WRITE)
@_noted
async def update_task(
    task: str, status: str | None = None, priority: str | None = None, append_note: str | None = None
) -> dict:
    """Change a task's status (backlog, todo, in_progress, in_review, blocked, done, cancelled) or priority (critical, high, medium, low), and/or append a dated note to the end of its description. This tool posts no comment: a task comment wakes the assigned agent and starts a paid run, so it is not offered here."""
    return await _board(task_board.update_task(_caller(WRITE_SCOPE), task, status, priority, append_note))


@mcp.tool(annotations=WRITE)
@_noted
async def use_browser(where: Literal["mac", "cloud"], session_id: str | None = None) -> dict:
    """Choose where a shared Claude Code or Codex session browses with its lightbulb-browser tools: "mac" (its Mac's Lightbulb agent Chrome, the default) or "cloud" (the workspace's cloud browser). Its next browser connection goes there; one already open stays where it is: to move an open one now, restart the lightbulb-browser MCP server (`/mcp` in Claude Code). Called from a shared session, omit session_id: it moves that session. Otherwise session_id comes from find_sessions; only people who may control that session can move it."""
    auth = _caller(WRITE_SCOPE)
    try:
        if device := _mac():
            source = get_access_token().claims.get("source") or {}
            if session_id is not None or not isinstance(source.get("sourceSessionId"), str):
                raise messaging.ApiProblem("INVALID_REQUEST", "From a shared session, omit session_id.")
            return await session_browser.place_from_mac(
                device.owner_id, device.user_id, device.device_id, source["sourceSessionId"], where
            )
        if session_id is None:
            raise messaging.ApiProblem("INVALID_REQUEST", "session_id is required.")
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


_REGISTERED_NAMES: dict[str, str] = {}


async def _registered_client(client_id: str) -> dict:
    """A dynamically registered client's own name ("Cursor"), as a token claim. Its client id is
    an opaque string; a metadata-document client's id is its URL and needs no lookup."""
    if client_id.startswith(("https://", "http://")):
        return {}
    if client_id not in _REGISTERED_NAMES:
        name = await clerk_admin.oauth_application_name(client_id)
        if name is None:
            return {}  # not cached: Clerk may answer next time
        _REGISTERED_NAMES[client_id] = name
    return {"clientName": _REGISTERED_NAMES[client_id]}


def client_identity(client_id: str, name: str | None = None) -> tuple[str, str]:
    """(slug, display name) for the OAuth client that holds the grant. A registered client is
    named by what it registered as; an opaque id is never shown."""
    if client_id in _CLIENTS:
        return _CLIENTS[client_id]
    if name:
        slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split()) or "app"
        return slug, name
    if not client_id.startswith(("https://", "http://")):
        return "app", "An app"
    host = urlsplit(client_id).hostname or client_id
    return _HOSTS.get(host, (host, host))


# The assistant apps' metadata-document hosts (claude.ai's connector, ChatGPT's apps).
_HOSTS = {
    "claude.ai": ("claude", "Claude"),
    "claude.com": ("claude", "Claude"),
    "chatgpt.com": ("chatgpt", "ChatGPT"),
}


def _sender(token) -> tuple[str, str]:
    """(slug, name) of what authors a send: the agent a key names, else the OAuth client."""
    if agent := (getattr(token, "claims", None) or {}).get("agentKey"):
        return f"agent-{agent['keyId']}", agent["name"]
    return client_identity(token.client_id, (getattr(token, "claims", None) or {}).get("clientName"))


NETWORK_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _network_request_id(request_id: str) -> str:
    if not NETWORK_REQUEST_ID.fullmatch(request_id):
        raise ToolError("INVALID_REQUEST: request_id is a new [A-Za-z0-9._-]{1,128} string.")
    return request_id


def _uuid_request_id(request_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(request_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError("INVALID_REQUEST: this destination takes a UUID request_id.") from exc


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _sent_key(auth: AuthContext, request_id: uuid.UUID | str) -> tuple[str, str]:
    # Under the workspace's OWNER# partition, so both wipes' delete_owner_partition removes it.
    return messaging_repo.owner_pk(auth.org_id), f"MCPSEND#{auth.user_id}#{request_id}"


async def _pin_destination(auth: AuthContext, request_id: uuid.UUID | str, destination: str, **extra) -> dict:
    """Remember where a request_id went, so message_receipt needs only the id and a reuse elsewhere fails."""
    pk, sk = _sent_key(auth, request_id)
    # No TTL: this is the receipt index and the cross-destination guard, like the room IDEMP row.
    item = {"pk": pk, "sk": sk, "destination": destination, **extra}
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


_STARTING = "RUNTIME_UNAVAILABLE: the workspace is starting; retry with the same request_id"


async def _through_member(auth: AuthContext, op: str, *, sender: tuple[str, str] | None = None, **fields):
    """A ``network.app`` call as this client (or ``sender``) via the signed-in person, its refusals
    as ToolErrors."""
    slug, client = sender or _sender(get_access_token())
    try:
        return await network_member.app_call(auth, slug, client, op, **fields)
    except asyncio.TimeoutError as exc:
        raise ToolError(_STARTING) from exc
    except messaging.ApiProblem as exc:
        if exc.error.code == "RUNTIME_UNAVAILABLE":
            raise ToolError(_STARTING) from exc
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
    except RuntimeError as exc:  # the member answered with an error: {"code", "message"}
        error = network_member.rpc_error(exc)
        code = error.get("code") or "UNAVAILABLE"
        # The bridge answers UNAVAILABLE while its network sidecar is off or starting.
        if code in ("UNAVAILABLE", "RUNTIME_UNAVAILABLE"):
            raise ToolError(_STARTING) from exc
        raise ToolError(f"{code}: {error.get('message') or 'the network refused the call'}") from exc


def _record(result, destination: str, request_id: uuid.UUID | str) -> dict:
    if not isinstance(result, dict):
        raise ToolError("UNAVAILABLE: the network answered in an unexpected shape.")
    return {**result, "destination": destination, "requestId": str(request_id)}


async def _posted(auth: AuthContext, room_id: str, request_id: uuid.UUID) -> bool:
    """This send already committed in that room (a retry replays it wherever the room went since)."""
    return (
        await messaging_repo._get(
            messaging_repo.room_pk(room_id), messaging_repo.human_idemp_sk(auth.user_id, str(request_id))
        )
        is not None
    )


@mcp.tool(annotations=SEND)
@_noted
async def send_message(
    text: str,
    request_id: str,
    session_id: str | None = None,
    reply_to: str | None = None,
    room_id: str | None = None,
    person_id: str | None = None,
    reply_to_message_id: uuid.UUID | None = None,
    mention_ids: list[str] | None = None,
    agent_id: str | None = None,
) -> dict:
    """Send as this client on the signed-in person's behalf (or, from a shared session, as that session) to exactly one of session_id, room_id, person_id (a member id; opens the DM if needed) or agent_id (a webhook agent from find_sessions agents: one webhook call; state delivered, failed or awaiting_approval). session_id comes from find_sessions: a network session_id (an address) sends on the team's lightbulb network through this workspace, and reply_to (the requestId of a message you got from check_inbox) answers it; reply_to alone replies to whoever sent that message. A shared session's sessionId (a UUID) delivers the text into it as a follow-up prompt it acts on (see canSend). request_id: a new [A-Za-z0-9._-]{1,128} string for a network send, a new UUID for any other; reuse it only to retry that same send. Room and person sends from a person may reply in a thread (reply_to_message_id) or address room members; a shared session's send takes neither. Cannot answer permission prompts."""
    auth = _caller(WRITE_SCOPE)
    request_id = str(request_id)
    network_to = None
    if (session_id is not None and not _is_uuid(session_id)) or (reply_to is not None and session_id is None):
        network_to, session_id = session_id, None
    targets = (
        ("room", room_id),
        ("person", person_id),
        ("session", session_id),
        ("agent", agent_id),
        ("network", network_to or reply_to),
    )
    if sum(target is not None for _, target in targets) != 1:
        raise messaging.ApiProblem(
            "INVALID_REQUEST", "Send to exactly one of session_id, room_id, person_id or agent_id."
        )
    if device := _mac():
        if network_to is not None or reply_to is not None:
            raise ToolError("INVALID_REQUEST: a shared session uses its own computer's network.")
        if reply_to_message_id or mention_ids:
            raise ToolError("INVALID_REQUEST: a session's send takes no thread or mentions.")
        kind, target = next((k, v) for k, v in targets if v is not None)
        return await _from_session(
            device,
            "send",
            {"destination": f"{kind}:{target}", "text": text, "requestId": str(_uuid_request_id(request_id))},
        )
    slug, client = _sender(get_access_token())
    if network_to is not None or reply_to is not None:
        if reply_to_message_id or mention_ids:
            raise messaging.ApiProblem("INVALID_REQUEST", "A network send takes no thread or mentions.")
        for value in (network_to, reply_to):
            if value is not None and (not value.strip() or len(value) > 256):
                raise messaging.ApiProblem("INVALID_REQUEST", "Invalid session_id or reply_to.")
        request_id = _network_request_id(request_id)
        destination = f"network:{network_to}" if network_to is not None else f"network:reply:{reply_to}"
        try:
            pinned = await _pin_destination(auth, request_id, destination, app=slug, client=client)
            if pinned.get("app") != slug:  # another app session would send it a second time
                raise messaging.ApiProblem("IDEMPOTENCY_CONFLICT", "request_id was used by another client.")
        except messaging.ApiProblem as exc:
            raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
        # The engine dedupes app.send on request_id per app session, so a retry sends once.
        fields = {"to": network_to, "text": text, "request_id": request_id}
        if reply_to is not None:
            fields["reply_to"] = reply_to
        result = await _through_member(auth, "send", sender=(slug, client), **fields)
        return _record(result, destination, request_id)
    request_id = _uuid_request_id(request_id)
    if agent_id is not None:
        if reply_to_message_id or mention_ids:
            raise messaging.ApiProblem("INVALID_REQUEST", "An agent send takes no thread or mentions.")
        agent_id = messaging.validate_uuid(agent_id, label="agent ID")
        try:
            await _pin_destination(auth, request_id, f"agent:{agent_id}")
            return await webhook_agents.send(auth, agent_id, text, str(request_id), via_client=client)
        except messaging.ApiProblem as exc:
            raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
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
            # A read: session_send holds a person the owner has not allowed until they Allow.
            target, _ = await local_sessions.authorize_session(auth, None, session_id)
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
        return {
            "sessionId": session_id,
            "requestId": str(request_id),
            "state": result["state"],
            "delivery": result["delivery"],
        }
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
            # A message author name holds 256 characters; a long profile name must not refuse the send.
            "displayName": f"{client} via {await messaging.human_display_name(auth.user_id)}"[:256],
        },
    )
    return {
        "roomId": str(message.room_id),
        "messageId": str(message.message_id),
        "messageSeq": message.message_seq,
        "state": "posted",
    }


@mcp.tool(annotations=READ)
@_noted
async def message_receipt(request_id: str) -> dict:
    """The receipt of a message you sent, by its request_id, including when the send response was lost."""
    auth = _caller()
    request_id = str(request_id)
    if device := _mac():
        return await _from_session(device, "receipt", {"requestId": str(_uuid_request_id(request_id))})
    # A UUID is pinned in its canonical text; a network id as given.
    request_id = str(uuid.UUID(request_id)) if _is_uuid(request_id) else _network_request_id(request_id)
    pinned = await messaging_repo._get(*_sent_key(auth, request_id))
    if pinned is None:
        return {"state": "unknown"}
    kind, target = pinned["destination"].split(":", 1)
    if kind == "person":
        if not pinned.get("roomId"):
            return {"state": "not_found"}  # the send stopped before its DM was known
        kind, target = "room", pinned["roomId"]
    if kind == "room":
        return await receipt_for(auth, target, uuid.UUID(request_id))
    if kind == "agent":
        return await webhook_agents.get_receipt(auth.org_id, auth.user_id, target, str(request_id)) or {
            "state": "unknown"
        }
    if kind == "network":
        # Read from the app session that sent it, whichever client asks.
        sender = (pinned["app"], pinned["client"])
        result = await _through_member(auth, "receipt", sender=sender, request_id=str(request_id))
        return _record(result, pinned["destination"], request_id)
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


@mcp.tool(annotations=READ)
@_noted
async def check_inbox(wait_secs: int = 0) -> dict:
    """Takes the messages sent to this client on the team's lightbulb network (as "<client> via <you>", through this workspace). Each message has its raw text and, in framed, the text as delivered; pass its requestId as send_message reply_to to answer it. wait_secs is accepted for the local tool's shape; this call answers at once. The first call from this client adds its address to the workspace."""
    auth = _caller()
    if _mac():  # a shared session's own computer is on the network itself
        raise ToolError("INVALID_REQUEST: a shared session uses its own computer's network.")
    if not 0 <= wait_secs <= 600:
        raise ToolError("INVALID_REQUEST: wait_secs is 0 to 600.")
    # Peek, build the answer, then ack: a call that fails before here loses nothing (the mail is
    # handed out again), at the cost of a repeat when only the ack is lost.
    answer = await _through_member(auth, "inbox", peek=True)
    records = answer.get("records") if isinstance(answer, dict) else None
    messages = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
    result = {"messages": messages}
    if ids := [m["id"] for m in messages if isinstance(m.get("id"), str)]:
        try:
            await _through_member(auth, "ack", ids=ids)
        except ToolError:  # an engine before app.ack took them already; otherwise they repeat
            logger.warning("network inbox ack failed; %d record(s) may repeat", len(ids))
    return result


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
