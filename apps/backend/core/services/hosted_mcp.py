"""Account-scoped Lightbulb MCP. Native session identity belongs to the Mac bridge."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from core.auth import AuthContext
from core.config import settings
from core.repositories import messaging_repo
from core.services import clerk_admin, local_sessions, messaging

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


@mcp.tool()
async def find_rooms(query: str = "", limit: int = 30) -> dict:
    """Find channels and existing DMs that the signed-in person may read."""
    auth = _caller()
    rooms = (await messaging.list_rooms(auth)).rooms
    needle = query.casefold().strip()
    matches = [
        room
        for room in rooms
        if not needle
        or needle in (room.name or "").casefold()
        or any(needle in member.display_name.casefold() for member in room.members)
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


@mcp.tool()
async def find_sessions(query: str = "", limit: int = 30) -> dict:
    """Find shared coding sessions by readable title, provider or folder; use sessionId to target one."""
    auth = _caller()
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
                "canSend": (
                    session.capabilities_verified
                    and session.control_unavailable_reason is None
                    and session.capabilities.send_mode != "none"
                    and session.status not in {"ended", "offline"}
                    and (
                        auth.user_id == session.human_user_id
                        or session.team_control
                        or auth.user_id in session.allowed_senders
                    )
                ),
            }
            for session in matches[: max(1, min(limit, 50))]
        ],
        "total": len(matches),
    }


@mcp.tool()
async def read_messages(room_id: str, after_seq: int | None = None, limit: int = 25) -> dict:
    """Read the latest page or continue after a message sequence in a room or DM."""
    auth = _caller()
    count = max(1, min(limit, 50))
    if after_seq is None:
        room = await messaging.get_room(auth, room_id)
        page = await messaging.list_messages(auth, room_id, 0, count, room.last_message_seq + 1)
    else:
        page = await messaging.list_messages(auth, room_id, max(0, after_seq), count)
    return page.model_dump(mode="json", by_alias=True)


@mcp.tool()
async def send_message(
    room_id: str,
    text: str,
    request_id: uuid.UUID,
    session_id: str | None = None,
    reply_to_message_id: uuid.UUID | None = None,
    mention_ids: list[str] | None = None,
) -> dict:
    """Send as the signed-in person; optionally reply in a thread or address room members and a session."""
    auth = _caller(WRITE_SCOPE)
    room = await messaging.get_room(auth, room_id)
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
            session_mentions=[session_id] if session_id else [],
            reply_to_message_id=uuid.UUID(reply_id) if reply_id else None,
            thread_root_message_id=uuid.UUID(thread_root_id) if thread_root_id else None,
        ),
        via="lightbulb_mcp",
    )
    return {
        "roomId": str(message.room_id),
        "messageId": str(message.message_id),
        "messageSeq": message.message_seq,
        "state": "posted",
        "sessionSends": [send.model_dump(mode="json", by_alias=True) for send in message.session_sends],
    }


@mcp.tool()
async def message_receipt(room_id: str, request_id: uuid.UUID) -> dict:
    """Reconcile a send by its request ID, including when the send response was lost."""
    auth = _caller()
    await messaging.require_room_member(auth.org_id, auth.user_id, room_id)
    receipt = await messaging_repo._get(
        messaging_repo.room_pk(room_id),
        messaging_repo.human_idemp_sk(auth.user_id, str(request_id)),
    )
    if receipt is None:
        return {"state": "not_found"}
    message = await messaging_repo.get_message(room_id, int(receipt["messageSeq"]))
    if message is None or message.get("via") != "lightbulb_mcp" or message.get("author", {}).get("id") != auth.user_id:
        return {"state": "not_found"}
    public = messaging.canonical_message(message).model_dump(mode="json", by_alias=True)
    for send in public.get("sessionSends", []):
        if send.get("requestId"):
            try:
                command = await local_sessions.get_command(auth, None, send["sessionId"], send["requestId"])
                send["state"] = command.state
            except messaging.ApiProblem:
                # The room message remains readable even if this session stopped sharing.
                pass
    return {"state": "posted", "message": public}


# API Gateway rewrites Host to the private ALB DNS name. The public OAuth URL is
# enforced by the audience check above; the API's existing CORS allowlist governs
# browser Origin access. The SDK still bounds POST bodies and validates JSON type.
app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
