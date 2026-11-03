"""Account-scoped Lightbulb MCP. A send is authored by the OAuth client, accountable to the person."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import time
import uuid
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
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl

from core.auth import AuthContext
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import messaging_repo
from core.services import (
    agent_keys,
    app_wake,
    clerk_admin,
    memory_api,
    memory_stages,
    messaging,
    network_member,
    network_route,
    task_board,
)

logger = logging.getLogger(__name__)

# Codex and Claude Code read these to decide which calls ask first; OpenAI's plugin review needs all
# three explicit on every tool. Only send_message is destructive (a sent message cannot be unsent).
# openWorld: a call that leaves the bounded workspace (other people act on it, or any URL / the web).
# They live here, not in network_tools.json: the engine serves that file byte for byte without hints.
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
OPEN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

READ_SCOPE = "lightbulb:messages:read"
WRITE_SCOPE = "lightbulb:messages:write"
RESOURCE = f"{settings.PUBLIC_API_BASE.rstrip('/')}/mcp"


class ClerkOAuthVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        resource: str = RESOURCE,
        required_scopes: set[str] | None = None,
        fallback_audience: tuple[str, frozenset[str]] | None = None,
    ):
        """``fallback_audience`` (audience, scopes): also accept that audience when the grant holds any of
        those scopes (the A2A door's stand-in while Clerk may not echo its resource)."""
        self.resource = resource
        self.required_scopes = {READ_SCOPE, "user:org:read"} if required_scopes is None else required_scopes
        self.fallback_audience = fallback_audience

    def _audience_ok(self, audience: list, scopes: list) -> bool:
        if self.resource in audience:
            return True
        if self.fallback_audience is None:
            return False
        aud, any_of = self.fallback_audience
        return aud in audience and bool(any_of.intersection(scopes))

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
                    or not isinstance(scopes, list)
                    or not all(isinstance(scope, str) for scope in scopes)
                    or not self._audience_ok(audience, scopes)
                    or not self.required_scopes.issubset(scopes)
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
                # A free account (or a grant with no workspace selected): the network tools only.
                org_id = None
            else:
                organizations = await clerk_admin.list_user_organizations(subject, limit=100, strict=True)
                if not any(org["id"] == org_id for org in organizations):
                    return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(expiration),
                resource=self.resource,
                subject=subject,
                claims={"org_id": org_id, "iss": settings.CLERK_ISSUER, **await _registered_client(client_id)},
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
            logger.warning("Lightbulb MCP OAuth verification failed")
            return None


class LightbulbVerifier(ClerkOAuthVerifier):
    """A person's caller: a Clerk opaque grant, or a per-agent key acting as its person. A coding
    session reaches these tools through its own computer's engine, not here."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(agent_keys.PREFIX):
            return await self._verify_agent_key(token)
        return await super().verify_token(token)

    async def _verify_agent_key(self, token: str) -> AccessToken | None:
        """A per-agent key acts as its person, only while they are still in the workspace."""
        try:
            row = await agent_keys.resolve(token)
            if row is None:
                return None
            # A free account's key is its own (ownerId is the user): the network tools only, and
            # only while that account is live (not deleted, banned or locked).
            personal = row["ownerId"] == row["userId"]
            if personal:
                user = await clerk_admin.get_user_strict(row["userId"])
                if not user or user.get("banned") or user.get("locked"):
                    return None
            organizations = (
                [] if personal else await clerk_admin.list_user_organizations(row["userId"], limit=100, strict=True)
            )
            if not personal and not any(org["id"] == row["ownerId"] for org in organizations):
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
                "org_id": None if personal else row["ownerId"],
                "iss": settings.CLERK_ISSUER,
                "agentKey": {"keyId": row["keyId"], "name": row["name"]},
            },
        )


_NEEDS_WORKSPACE = (
    "WORKSPACE_REQUIRED: this needs a Lightbulb workspace. The network tools (find_sessions, "
    "send_message to a session_id, check_inbox, message_receipt, wake_me) work without one."
)


def _caller(scope: str = READ_SCOPE) -> AuthContext:
    """A workspace's caller: every tool but the network ones needs a selected workspace."""
    auth = _net_caller(scope)
    if not auth.org_id:
        raise ToolError(_NEEDS_WORKSPACE)
    return auth


def _net_caller(scope: str = READ_SCOPE) -> AuthContext:
    """The network tools' caller: a workspace's, or a free account's (``org_id`` None)."""
    token = get_access_token()
    if token is None or scope not in token.scopes or not token.subject or not token.claims:
        raise PermissionError(f"Lightbulb OAuth scope {scope} required")
    org_id = token.claims.get("org_id")
    return AuthContext(
        user_id=token.subject,
        org_id=org_id if isinstance(org_id, str) else None,
        verified_issuer=token.claims.get("iss"),
    )


# API Gateway cuts a request at 30 s: a hosted tool answers within this budget, its network
# calls included (container_rpc's own floor is 90 s, for a cold start this answer never waits on).
HOSTED_BUDGET_S = 24.0
# check_inbox waits for mail at most this long, keeping the rest of the budget for peek and ack.
INBOX_WAIT_CAP_S = 20.0
_RESERVE_S = 3.0  # what a peek, an ack or the unread line may take on a warm workspace
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("hosted_mcp_deadline", default=None)
_NETWORK_TOOLS = frozenset(
    {
        "find_sessions",
        "find_workers",
        "send_message",
        "assign",
        "cancel_assignment",
        "message_receipt",
        "check_inbox",
        "wake_me",
    }
)
_sleep = asyncio.sleep  # a seam for tests


def _remaining() -> float:
    deadline = _deadline.get()
    return HOSTED_BUDGET_S if deadline is None else deadline - time.monotonic()


async def network_unread_note() -> str | None:
    """The engine's unread line for this client's app session on the network (a hosted client
    is pull-only), from its pinned computer. Never fails the tool it rides on."""
    auth = _net_caller()
    slug, client = _sender(get_access_token())
    try:
        route = await _route(auth, slug, client)
        result = await network_route.app_call(auth.user_id, slug, client, "unread", _RESERVE_S, route=route)
    except (RuntimeError, messaging.ApiProblem, asyncio.TimeoutError, ToolError):
        logger.debug("network unread refused user=%s", auth.user_id)
        return None
    count = int(result.get("unread") or 0) if isinstance(result, dict) else 0
    if not count:
        return None
    return f"{count} unread message{'' if count == 1 else 's'} for this session; call check_inbox to read them."


def _noted(tool):
    """A hosted client's network tools carry its network mail's unread line. Each call runs within
    ``HOSTED_BUDGET_S``, and the note never fails a tool."""
    network = tool.__name__ in _NETWORK_TOOLS

    @functools.wraps(tool)
    async def call(*args, **kwargs):
        token = _deadline.set(time.monotonic() + HOSTED_BUDGET_S)
        try:
            result = await tool(*args, **kwargs)
            if not isinstance(result, dict):
                return result
            try:
                note = None
                if network and _remaining() > 1.0:
                    note = await asyncio.wait_for(network_unread_note(), min(_RESERVE_S, _remaining()))
                if note:
                    result = {**result, "unread": note}
            except asyncio.TimeoutError:
                logger.debug("network unread note timed out")
            except Exception:  # noqa: BLE001 -- a note never fails the tool it rides on
                logger.warning("unread note unavailable", exc_info=True)
            return result
        finally:
            _deadline.reset(token)

    return call


# The tool definitions the engine's `lightbulb` MCP serves too (its src/mcp/tools.json, vendored at the
# pinned engine tag): names, descriptions and input schemas. wake_me is hosted-only.
SPEC = {tool["name"]: tool for tool in json.loads((Path(__file__).with_name("network_tools.json")).read_text())}

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


# The reads below take an AuthContext so the engine's workspace routes (routers/messaging.py,
# /local-sessions/device/*, as a signed-in computer's person) serve the same answers.
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


@mcp.tool(annotations=READ, description=SPEC["find_rooms"]["description"])
@_noted
async def find_rooms(query: str = "", limit: int = 30) -> dict:
    return await rooms_for(_caller(), query, limit)


@mcp.tool(annotations=READ, description=SPEC["find_sessions"]["description"])
@_noted
async def find_sessions(query: str = "", limit: int = 30) -> dict:
    found = await _network_sessions(_net_caller(), query)
    return {"sessions": found[: max(1, min(limit, 50))], "total": len(found)}


def _always_on(session_id: str) -> bool:
    """A name address after the member prefix: no ``:`` and not ``terminal``. Mirrors ``always_on``
    in the engine's ``src/mcp.rs``."""
    address = session_id.partition("/")[2]
    return ":" not in address and address != "terminal"


def _worker_host(row: dict) -> bool:
    return row.get("kind") == "worker" or row.get("free") is not None


@mcp.tool(annotations=READ, description=SPEC["find_workers"]["description"])
@_noted
async def find_workers(query: str = "", limit: int = 20) -> dict:
    found = await _network_sessions(_net_caller(), query)
    online = [row for row in found if row.get("online") is True]
    # Computers that start fresh workers first, then always-on agents: the engine's ``always_on``.
    # A worker host is the row with ``free``: the app link strips ``kind`` (``link.rs``
    # ``this_computers_own_kept``), and the engine sets ``free`` only on worker offers.
    hosts = [{**row, "always_on": False} for row in online if _worker_host(row)]
    named = [{**row, "always_on": True} for row in online if not _worker_host(row) and _always_on(row["session_id"])]
    kept = hosts + named
    return {"sessions": kept[: max(1, min(limit, 50))], "total": len(kept)}


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


@mcp.tool(annotations=READ, description=SPEC["read_messages"]["description"])
@_noted
async def read_messages(room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    return await messages_for(_caller(), room_id, after_seq, limit)


async def _board(call):
    """FastMCP hides any non-ToolError text, so a refused board call says why."""
    try:
        return await call
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc


@mcp.tool(annotations=READ, description=SPEC["find_tasks"]["description"])
@_noted
async def find_tasks(query: str = "", status: str = "", assignee: str = "", limit: int = 25) -> dict:
    return await _board(task_board.find_tasks(_caller(), query, status, assignee, limit))


@mcp.tool(annotations=READ, description=SPEC["read_task"]["description"])
@_noted
async def read_task(task: str, comments: int = 10) -> dict:
    return await _board(task_board.read_task(_caller(), task, comments))


@mcp.tool(annotations=WRITE, description=SPEC["update_task"]["description"])
@_noted
async def update_task(
    task: str, status: str | None = None, priority: str | None = None, append_note: str | None = None
) -> dict:
    return await _board(task_board.update_task(_caller(WRITE_SCOPE), task, status, priority, append_note))


async def _memory(call):
    """FastMCP hides any non-ToolError text: a bad question or a failed memory says why."""
    try:
        return await call
    except (ValueError, RuntimeError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ, description=SPEC["recall"]["description"])
@_noted
async def recall(question: str) -> dict:
    auth = _caller()
    page = await _memory(memory_api.recall_page(auth.org_id, auth.user_id, question))
    # The one page an agent reads (8,000-9,000 tokens for English text), as the CLI's tool:
    # the structured results stay for the Memory tab.
    turns = sum(r["kind"] == "turn" for r in page["results"])
    return {"page": memory_stages.page_text(page), "counts": {"facts": len(page["results"]) - turns, "turns": turns}}


@mcp.tool(annotations=WRITE, description=SPEC["remember"]["description"])
@_noted
async def remember(
    subject: str,
    fact: str,
    kind: Literal["fact", "decision", "lesson", "preference"],
    scope: Literal["me", "team"] = "me",
    replaces: list[str] | None = None,
) -> dict:
    auth = _caller(WRITE_SCOPE)
    return await _memory(
        memory_api.remember(
            auth.org_id,
            subject=subject,
            fact=fact,
            kind=kind,
            scope=scope,
            replaces=replaces,
            on_behalf_of=auth.user_id,
        )
    )


# `use_browser` is left out: it moves a coding session's browser, and a hosted app has none.


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


def _uuid_request_id(auth: AuthContext, request_id: str) -> uuid.UUID:
    """A room or person send's id: a UUID as given, or one derived from a [A-Za-z0-9._-]{1,128}
    request_id (per person, so a retry of that send reuses it), as the engine derives its own."""
    if _is_uuid(request_id):
        return uuid.UUID(request_id)
    _network_request_id(request_id)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-mcp:{auth.user_id}:{request_id}")


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _sent_key(auth: AuthContext, request_id: uuid.UUID | str) -> tuple[str, str]:
    # Under the workspace's OWNER# partition, so both wipes' delete_owner_partition removes it;
    # a free account's under its own NETAPP# partition.
    pk = messaging_repo.owner_pk(auth.org_id) if auth.org_id else f"NETAPP#{auth.user_id}"
    return pk, f"MCPSEND#{auth.user_id}#{request_id}"


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


async def person_dm(auth: AuthContext, member_id: str, *, create_key: str | None = None) -> str:
    """The caller's DM room with this member, opened when missing (``create_key``: the send's request
    id), the same find-or-create the web uses. Reuse comes from the lookup. The create id is per
    operation, never per pair: a pair id would replay the first DM's create receipt after that DM
    became a group DM, and post to the group."""
    if member_id == auth.user_id:
        raise messaging.ApiProblem("INVALID_REQUEST", "Cannot message your own person destination.")
    rooms = (await messaging.list_rooms(auth)).rooms
    match = next((r for r in rooms if r.kind == "dm" and any(m.id == member_id for m in r.members)), None)
    if match is not None:
        return str(match.room_id)
    if create_key is None:
        raise messaging.ApiProblem("ROOM_NOT_FOUND", "Open a direct message with this person first.")
    # People are Clerk ids; anything else is an agent, which create_room checks against the roster.
    counterpart = (
        messaging.CreateRoomMember(kind="human", id=member_id, role="member", listen_mode=None)
        if member_id.startswith("user_")
        else messaging.CreateRoomMember(kind="agent", id=member_id, role="member", listen_mode="active")
    )
    room = await messaging.create_room(
        auth,
        messaging.CreateRoomRequest(
            # Per send, so a retry of this send replays this create and nothing older.
            client_room_id=uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-dm:{auth.user_id}:{member_id}:{create_key}"),
            name=None,
            kind="dm",
            members=[counterpart],
        ),
    )
    return str(room.room_id)


async def _person_room(auth: AuthContext, request_id: uuid.UUID, person_id: str, *, create: bool) -> str:
    """The DM a person send goes to, pinned on its record once known: a retry or receipt keeps that
    room even after it grows into a group DM or a newer DM appears."""
    pinned = await _pin_destination(auth, request_id, f"person:{person_id}")
    if pinned.get("roomId"):
        return pinned["roomId"]
    room_id = await person_dm(auth, person_id, create_key=str(request_id) if create else None)
    pk, sk = _sent_key(auth, request_id)
    updated = await run_in_thread(
        messaging_repo._table().update_item,
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET roomId = if_not_exists(roomId, :room)",
        ExpressionAttributeValues={":room": room_id},
        ReturnValues="ALL_NEW",
    )
    return updated["Attributes"]["roomId"]


async def _through_member(auth: AuthContext, op: str, *, sender: tuple[str, str] | None = None, **fields):
    """An app op as this client (or ``sender``) via the signed-in person's pinned computer, its
    refusals as ToolErrors."""
    slug, client = sender or _sender(get_access_token())
    remaining = _remaining()
    if remaining <= 0.5:  # past the budget: the client retries
        raise ToolError(_SLOW)
    route = await _route(auth, slug, client)
    return await _through_computer(auth, slug, client, op, remaining, route, **fields)


_SLOW = "UNAVAILABLE: your computer did not answer in time; retry with the same request_id"


async def _route(auth: AuthContext, slug: str, client: str) -> dict:
    """This app connection's pinned computer (``network_route.route_for``). Its refusals as
    ToolErrors."""
    try:
        return await network_route.route_for(auth, get_access_token().client_id, slug, client)
    except messaging.ApiProblem as exc:
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
    except Exception as exc:  # noqa: BLE001 -- DynamoDB could not say
        raise ToolError("UNAVAILABLE: Lightbulb could not look up this app's computer; retry") from exc


async def _through_computer(
    auth: AuthContext, slug: str, client: str, op: str, remaining: float, route: dict, **fields
):
    """The same call through the app's pinned computer (never another)."""
    try:
        return await network_route.app_call(auth.user_id, slug, client, op, remaining - 0.5, route=route, **fields)
    except asyncio.TimeoutError as exc:
        raise ToolError(_SLOW) from exc
    except messaging.ApiProblem as exc:
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
    except RuntimeError as exc:
        error = network_member.rpc_error(exc)
        code = error.get("code") or "UNAVAILABLE"
        if code in ("UNAVAILABLE", "RUNTIME_UNAVAILABLE"):
            raise ToolError("UNAVAILABLE: your computer's link closed; retry with the same request_id") from exc
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


@mcp.tool(annotations=SEND, description=SPEC["send_message"]["description"])
@_noted
async def send_message(
    text: str,
    request_id: str,
    session_id: str | None = None,
    reply_to: str | None = None,
    room_id: str | None = None,
    person_id: str | None = None,
    reply_to_message_id: str | None = None,
    mention_ids: list[str] | None = None,
    status: Literal["done", "failed", "cancelled"] | None = None,
) -> dict:
    auth = _net_caller(WRITE_SCOPE)
    request_id = str(request_id)
    # reply_to names its own destination; with a session_id it is the network reply to that session.
    network = session_id if session_id is not None else reply_to
    if sum(target is not None for target in (network, room_id, person_id)) != 1:
        raise messaging.ApiProblem(
            "INVALID_REQUEST", "Send to exactly one of session_id, reply_to, room_id or person_id."
        )
    if status is not None and reply_to is None:
        raise messaging.ApiProblem("INVALID_REQUEST", "status answers an assignment: send it with reply_to.")
    slug, client = _sender(get_access_token())
    if network is not None:
        if reply_to_message_id or mention_ids:
            raise messaging.ApiProblem("INVALID_REQUEST", "A network send takes no thread or mentions.")
        for value in (session_id, reply_to):
            if value is not None and (not value.strip() or len(value) > 256):
                raise messaging.ApiProblem("INVALID_REQUEST", "Invalid session_id or reply_to.")
        request_id = _network_request_id(request_id)
        destination = f"network:{session_id}" if session_id is not None else f"network:reply:{reply_to}"
        try:
            pinned = await _pin_destination(auth, request_id, destination, app=slug, client=client)
            if pinned.get("app") != slug:  # another app session would send it a second time
                raise messaging.ApiProblem("IDEMPOTENCY_CONFLICT", "request_id was used by another client.")
        except messaging.ApiProblem as exc:
            raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
        # The engine dedupes app.send on request_id per app session, so a retry sends once.
        fields = {"to": session_id, "text": text, "request_id": request_id}
        if reply_to is not None:
            fields["reply_to"] = reply_to
        if status is not None:
            fields["status"] = status
            fields["kind"] = "result"
        result = await _through_member(auth, "send", sender=(slug, client), **fields)
        return _record(result, destination, request_id)
    if not auth.org_id:
        raise ToolError(_NEEDS_WORKSPACE)
    request_id = _uuid_request_id(auth, request_id)
    if reply_to_message_id is not None:
        reply_to_message_id = messaging.validate_uuid(reply_to_message_id, label="reply_to_message_id")
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
    reply_id = reply_to_message_id
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


async def _network_forward(auth: AuthContext, request_id: str, destination: str, **fields) -> dict:
    """Pin ``request_id`` to its destination, then forward ``fields`` as an ``app.send`` (the
    engine dedupes on request_id per app session, so a retry sends once)."""
    slug, client = _sender(get_access_token())
    request_id = _network_request_id(request_id)
    try:
        pinned = await _pin_destination(auth, request_id, destination, app=slug, client=client)
        if pinned.get("app") != slug:  # another app session would send it a second time
            raise messaging.ApiProblem("IDEMPOTENCY_CONFLICT", "request_id was used by another client.")
    except messaging.ApiProblem as exc:
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
    result = await _through_member(auth, "send", sender=(slug, client), request_id=request_id, **fields)
    return _record(result, destination, request_id)


def _checked_id(value: str, label: str) -> str:
    if not value.strip() or len(value) > 256:
        raise messaging.ApiProblem("INVALID_REQUEST", f"Invalid {label}.")
    return value


@mcp.tool(annotations=SEND, description=SPEC["assign"]["description"])
@_noted
async def assign(
    request_id: str,
    to: str,
    brief: str,
    done_when: str,
    deadline_s: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> dict:
    auth = _net_caller(WRITE_SCOPE)
    _checked_id(to, "to")
    # The brief, done_when and deadline_s bounds are the engine's: it answers bad_request.
    fields = {"to": to, "text": brief, "kind": "assign", "done_when": done_when}
    for key, value in (("deadline_s", deadline_s), ("repo", repo), ("branch", branch)):
        if value is not None:
            fields[key] = value
    return await _network_forward(auth, str(request_id), f"network:{to}", **fields)


@mcp.tool(annotations=SEND, description=SPEC["cancel_assignment"]["description"])
@_noted
async def cancel_assignment(assignment: str, request_id: str, note: str | None = None) -> dict:
    auth = _net_caller(WRITE_SCOPE)
    _checked_id(assignment, "assignment")
    # No `to`: the engine addresses a cancel to the assignment's target.
    return await _network_forward(
        auth,
        str(request_id),
        f"network:reply:{assignment}",
        reply_to=assignment,
        text=note or "cancelled",
        kind="cancel",
    )


@mcp.tool(annotations=READ, description=SPEC["message_receipt"]["description"])
@_noted
async def message_receipt(request_id: str) -> dict:
    auth = _net_caller()
    request_id = str(request_id)
    # A network id is pinned as given; a room or person send under its UUID.
    pinned = None
    if NETWORK_REQUEST_ID.fullmatch(request_id):
        pinned = await messaging_repo._get(*_sent_key(auth, request_id))
    if pinned is None:
        # A room or person send is pinned under its UUID (given, or derived from the id).
        request_id = str(_uuid_request_id(auth, request_id))
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
    if kind == "network":
        # Read from the app session that sent it, whichever client asks.
        sender = (pinned["app"], pinned["client"])
        result = await _through_member(auth, "receipt", sender=sender, request_id=str(request_id))
        return _record(result, pinned["destination"], request_id)
    return {"state": "unknown"}  # a send to a destination this server no longer takes


@mcp.tool(annotations=READ, description=SPEC["check_inbox"]["description"])
@_noted
async def check_inbox(wait_secs: int = 0) -> dict:
    auth = _net_caller()
    if not 0 <= wait_secs <= 600:
        raise ToolError("INVALID_REQUEST: wait_secs is 0 to 600.")
    if wait_secs:
        # Polls the cheap count, not the inbox, and stops early enough for the peek and ack.
        until = min(time.monotonic() + min(wait_secs, INBOX_WAIT_CAP_S), time.monotonic() + _remaining() - _RESERVE_S)
        delay = 1.0
        while True:
            count = await _through_member(auth, "unread")
            left = until - time.monotonic()
            if (isinstance(count, dict) and count.get("unread")) or left <= 0:
                break
            await _sleep(min(delay, left))
            delay = min(delay * 1.5, 3.0)
    # Peek, build the answer, then ack: a call that fails before here loses nothing (the mail is
    # handed out again), at the cost of a repeat when only the ack is lost.
    answer = await _through_member(auth, "inbox", peek=True)
    records = answer.get("records") if isinstance(answer, dict) else None
    messages = [r for r in records if isinstance(r, dict)][:_ACK_MAX] if isinstance(records, list) else []
    result = {"messages": messages}
    ids = [m["id"] for m in messages if isinstance(m.get("id"), str)]
    if ids and _remaining() < _RESERVE_S:
        # A late peek may already be past API Gateway's cut: leave the mail to be handed out again.
        logger.info("network inbox ack skipped (late); %d record(s) repeat", len(ids))
    elif ids:
        try:
            await _through_member(auth, "ack", ids=ids)
        except ToolError:  # an engine before app.ack took them already; otherwise they repeat
            logger.warning("network inbox ack failed; %d record(s) may repeat", len(ids))
    return result


_ACK_MAX = 100  # the engine's app.ack limit; the rest wait for the next call


@mcp.tool(annotations=OPEN)
@_noted
async def wake_me(webhook_url: str) -> dict:
    """For a bot that cannot keep calling check_inbox (a Grok routine, a Muse agent): give the https URL of the webhook that starts you, and Lightbulb POSTs it when new network mail arrives for this client (content-free: you then call check_inbox). Its key is never given here: your person pastes it on the Lightbulb page this answer names. Calling it again replaces the URL (and needs the key again)."""
    auth = _net_caller(WRITE_SCOPE)
    slug, client = _sender(get_access_token())
    try:
        return await app_wake.request(auth.user_id, slug, client, webhook_url)
    except messaging.ApiProblem as exc:
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc


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


# Network workers are hidden, like the engine's `network_workers`: off, a hosted assistant is
# offered none of the assignment tools.
WORKER_TOOLS = ("find_workers", "assign", "cancel_assignment")
if not settings.HOSTED_NETWORK_WORKERS:
    for _name in WORKER_TOOLS:
        mcp.remove_tool(_name)
