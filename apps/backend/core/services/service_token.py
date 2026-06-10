"""OpenClaw service-token JWTs.

These are long-lived JWTs minted by the backend, signed with a symmetric secret
shared with the Lambda Authorizer. They authorize Paperclip agents to reach a
specific user's OpenClaw container via the existing WebSocket gateway.

Format: HS256 JWT
Claims:
  - sub: user_id (string)
  - kind: "paperclip_service" (string)
  - iat: issued-at (int)
  - exp: expiry (int)  — default 1 year
  - jti: unique token id (string)  — for future revocation
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

SERVICE_TOKEN_KIND = "paperclip_service"
DEFAULT_TTL_DAYS = 365


def _signing_key() -> str:
    key = os.environ.get("PAPERCLIP_SERVICE_TOKEN_KEY")
    if not key:
        raise RuntimeError("PAPERCLIP_SERVICE_TOKEN_KEY env var not set")
    return key


def mint(user_id: str, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """Mint a service-token JWT for the given user_id."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "kind": SERVICE_TOKEN_KIND,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify(token: str) -> dict:
    """Verify a service token. Returns the claims dict on success.

    Raises jwt.ExpiredSignatureError, jwt.InvalidTokenError on failure.
    """
    claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    if claims.get("kind") != SERVICE_TOKEN_KIND:
        raise jwt.InvalidTokenError(f"Wrong kind claim: {claims.get('kind')!r}")
    if not claims.get("sub"):
        raise jwt.InvalidTokenError("Missing sub claim")
    return claims
