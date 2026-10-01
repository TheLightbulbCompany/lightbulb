"""Account-scoped Lightbulb MCP. Native session identity belongs to the Mac bridge."""

from __future__ import annotations

import logging
import time
import uuid

import httpx
import jwt
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from core.auth import AuthContext, _find_rsa_key, _get_cached_jwks
from core.config import settings
from core.repositories import messaging_repo
from core.services import clerk_admin, messaging

logger = logging.getLogger(__name__)

READ_SCOPE = "lightbulb:messages:read"
WRITE_SCOPE = "lightbulb:messages:write"
RESOURCE = f"{settings.PUBLIC_API_BASE.rstrip('/')}/mcp"


class ClerkOAuthVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify revocation with Clerk, then signature, audience, org and live membership."""
        if not settings.CLERK_SECRET_KEY:
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
            if not isinstance(verified, dict):
                return None
            if verified.get("active") is False or verified.get("revoked") or verified.get("expired"):
                return None
            subject = verified.get("subject")
            client_id = verified.get("client_id")
            scopes = verified.get("scopes")
            expiration = verified.get("expiration")
            if (
                not isinstance(subject, str)
                or not subject.startswith("user_")
                or not isinstance(client_id, str)
                or not client_id
                or not isinstance(scopes, list)
                or not all(isinstance(scope, str) for scope in scopes)
                or READ_SCOPE not in scopes
                or "user:org:read" not in scopes
                or not isinstance(expiration, (int, float))
                or expiration <= time.time()
            ):
                return None

            # Clerk's verify endpoint does not expose the selected org or audience.
            # Require a signed JWT access token with both claims; an opaque token
            # cannot pass this gate until Clerk offers equivalent introspection.
            key_id = jwt.get_unverified_header(token)["kid"]
            key = _find_rsa_key(
                await _get_cached_jwks(f"{settings.CLERK_ISSUER}/.well-known/jwks.json"), key_id
            )
            if key is None:
                return None
            claims = jwt.decode(
                token,
                jwt.PyJWK(key).key,
                algorithms=["RS256"],
                issuer=settings.CLERK_ISSUER,
                audience=RESOURCE,
                leeway=30,
            )
            org_id = claims.get("org_id")
            if claims.get("sub") != subject or not isinstance(org_id, str) or not org_id.startswith("org_"):
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
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError, RuntimeError):
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
                "members": [m.display_name for m in room.members],
            }
            for room in matches[: max(1, min(limit, 50))]
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
async def send_message(room_id: str, text: str, request_id: str) -> dict:
    """Send as the signed-in person to an existing channel or DM. Reuse request_id on retries."""
    auth = _caller(WRITE_SCOPE)
    message = await messaging.create_human_message(
        auth,
        room_id,
        messaging.CreateMessageRequest(
            client_message_id=uuid.UUID(request_id),
            text=text,
            mentions=[],
            session_mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="lightbulb_mcp",
    )
    return {
        "roomId": str(message.room_id),
        "messageId": str(message.message_id),
        "messageSeq": message.message_seq,
        "state": "posted",
    }


@mcp.tool()
async def message_receipt(room_id: str, request_id: str) -> dict:
    """Reconcile a send by its request ID, including when the send response was lost."""
    auth = _caller()
    await messaging.require_room_member(auth.org_id, auth.user_id, room_id)
    receipt = await messaging_repo._get(
        messaging_repo.room_pk(room_id),
        messaging_repo.human_idemp_sk(auth.user_id, str(uuid.UUID(request_id))),
    )
    if receipt is None:
        return {"state": "not_found"}
    message = await messaging_repo.get_message(room_id, int(receipt["messageSeq"]))
    if (
        message is None
        or message.get("via") != "lightbulb_mcp"
        or message.get("author", {}).get("id") != auth.user_id
    ):
        return {"state": "not_found"}
    return {"state": "posted", "message": messaging.canonical_message(message).model_dump(mode="json", by_alias=True)}


# API Gateway rewrites Host to the private ALB DNS name. The public OAuth URL is
# enforced by the audience check above; the API's existing CORS allowlist governs
# browser Origin access. The SDK still bounds POST bodies and validates JSON type.
app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
