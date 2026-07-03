"""Dual auth for the platform operator's read-only admin debug surface.

The founder's agent holds a platform_ops token (see platform_ops_token.py)
bound to settings.PLATFORM_OPS_OWNER_ID. Beyond the /ops/* surface, that
token may READ a curated set of /admin/* debug endpoints — never mutate.
Mutations stay behind require_platform_admin (Clerk) only, so a leaked
agent token can inspect state but cannot act on it.

Default-deny: when PLATFORM_OPS_OWNER_ID is unset there is no owner to bind
the token to, so the token path is skipped entirely and only the Clerk
platform-admin path can authenticate.
"""

from __future__ import annotations

import logging

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import AuthContext, get_current_user, require_platform_admin
from core.config import settings
from core.services import platform_ops_token

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


def verify_platform_ops_token(token: str) -> dict | None:
    """Return claims when ``token`` is a platform_ops token bound to the configured owner.

    None on any verification failure — callers fall through to their Clerk
    path. Fail-closed: an unset PLATFORM_OPS_OWNER_ID means no owner binding
    exists, so every token is rejected here regardless of its validity.
    """
    owner = settings.PLATFORM_OPS_OWNER_ID
    if not owner:
        return None
    try:
        return platform_ops_token.verify(token, expected_owner_id=owner)
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError, RuntimeError):
        return None


async def require_admin_read_access(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """Allow Clerk platform-admins OR the platform_ops owner token. Default-deny.

    Read-only endpoints ONLY — anything that mutates must stay on
    require_platform_admin so the agent token can never write.

    On the token path the returned AuthContext is synthetic: user_id is the
    token's sub (the platform owner id), and there is no email/org claim —
    nothing on the read surface requires one.
    """
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing credentials")

    # Agent path first — cheap local HS256 verify, no network.
    claims = verify_platform_ops_token(creds.credentials)
    if claims is not None:
        # The platform-ops read trail. Clerk admins are already attributed
        # via bind_request_context inside get_current_user; the agent path
        # gets one structured line per authenticated read instead.
        logger.info(
            "platform_ops admin read: path=%s sub=%s jti=%s",
            request.url.path,
            claims.get("sub"),
            claims.get("jti"),
        )
        return AuthContext(user_id=claims["sub"])

    # Admin path — Clerk JWT. get_current_user raises 401 on invalid tokens
    # (503 when JWKS is unreachable); let those surface unchanged so Clerk
    # callers see the same errors as on every other admin endpoint.
    auth = await get_current_user(credentials=creds)
    return require_platform_admin(auth)
