"""Who calls the A2A door: a Lightbulb session JWT, or an outside client's Clerk OAuth grant for the
/a2a audience. Both resolve to a person in a workspace; the grant also names its client and scopes."""

from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core.auth import AuthContext, get_current_user, security
from core.config import settings
from core.services import hosted_mcp

RESOURCE_A2A = f"{settings.PUBLIC_API_BASE.rstrip('/')}/a2a"
SEND_SCOPE = "lightbulb:a2a:send"
READ_SCOPE_A2A = "lightbulb:a2a:read"
A2A_SCOPES = frozenset({SEND_SCOPE, READ_SCOPE_A2A})


@dataclass(frozen=True)
class A2ACaller:
    auth: AuthContext
    verified_by: Literal["clerk-session", "clerk-oauth"]
    client_id: str | None
    scopes: frozenset[str]


def session_caller(auth: AuthContext) -> A2ACaller:
    """A signed-in person: both A2A scopes."""
    return A2ACaller(auth=auth, verified_by="clerk-session", client_id=None, scopes=A2A_SCOPES)


def _verifier() -> hosted_mcp.ClerkOAuthVerifier:
    # Per call: the fallback follows the live setting. Per-agent keys (LightbulbVerifier) are not accepted.
    fallback = (hosted_mcp.RESOURCE, A2A_SCOPES) if settings.A2A_ACCEPT_MCP_AUDIENCE else None
    return hosted_mcp.ClerkOAuthVerifier(
        resource=RESOURCE_A2A, required_scopes={"user:org:read"}, fallback_audience=fallback
    )


async def a2a_caller(credentials: HTTPAuthorizationCredentials = Depends(security)) -> A2ACaller:
    token = credentials.credentials
    if "." in token:  # a JWT: the signed-in session path
        return session_caller(await get_current_user(credentials))
    access = await _verifier().verify_token(token)
    if access is None or not access.subject:
        raise HTTPException(401, detail={"code": "invalid_token"})
    org_id = (access.claims or {}).get("org_id")
    if not org_id:
        raise HTTPException(403, detail={"code": "workspace_required", "message": "Select a workspace."})
    return A2ACaller(
        auth=AuthContext(user_id=access.subject, org_id=org_id, verified_issuer=access.claims.get("iss")),
        verified_by="clerk-oauth",
        client_id=access.client_id,
        scopes=frozenset(access.scopes),
    )


def require_scope(caller: A2ACaller, scope: str) -> None:
    if scope not in caller.scopes:
        raise HTTPException(403, detail={"code": "scope_required", "message": f"This needs the {scope} scope."})


_endpoints: dict[str, str] = {}  # Clerk's authorize and token URLs, once fetched


async def _clerk_metadata() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{settings.CLERK_ISSUER.rstrip('/')}/.well-known/oauth-authorization-server")
        response.raise_for_status()
        return response.json()


async def oauth_endpoints() -> dict[str, str]:
    """``authorization_url`` and ``token_url`` for the card; Clerk's usual paths when its metadata is unreachable."""
    if not _endpoints:
        try:
            meta = await _clerk_metadata()
            urls = {"authorization_url": meta["authorization_endpoint"], "token_url": meta["token_endpoint"]}
            if not all(isinstance(u, str) and u for u in urls.values()):
                raise ValueError("bad metadata")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            issuer = settings.CLERK_ISSUER.rstrip("/")
            return {"authorization_url": f"{issuer}/oauth/authorize", "token_url": f"{issuer}/oauth/token"}
        _endpoints.update(urls)
    return _endpoints
