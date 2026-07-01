"""platform_ops service tokens.

Owner-scoped JWTs the backend mints so the founder's agent (Friday) can call
the ops API from its container over outbound HTTPS. Distinct from
service_token.py (paperclip_service) in three deliberate ways:
  1. A dedicated signing key (PLATFORM_OPS_TOKEN_KEY) — independently revocable.
  2. A distinct `kind` ("platform_ops") — a tenant paperclip_service token is
     rejected on the ops path.
  3. verify() enforces `sub == expected_owner_id` — the token is bound to the
     single platform owner, so it grants nothing cross-tenant.

Format: HS256 JWT. Claims: sub (owner_id), kind, iat, exp, jti.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

PLATFORM_OPS_TOKEN_KIND = "platform_ops"
DEFAULT_TTL_DAYS = 365


def _signing_key() -> str:
    key = os.environ.get("PLATFORM_OPS_TOKEN_KEY")
    if not key:
        raise RuntimeError("PLATFORM_OPS_TOKEN_KEY env var not set")
    return key


def mint(owner_id: str, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """Mint a platform_ops token bound to owner_id."""
    if not owner_id:
        raise ValueError("owner_id is required")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": owner_id,
        "kind": PLATFORM_OPS_TOKEN_KIND,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify(token: str, expected_owner_id: str) -> dict:
    """Verify a platform_ops token bound to expected_owner_id.

    Raises jwt.ExpiredSignatureError on expiry, jwt.InvalidTokenError on wrong
    kind / empty sub / owner mismatch / bad signature.
    """
    claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    if claims.get("kind") != PLATFORM_OPS_TOKEN_KIND:
        raise jwt.InvalidTokenError(f"Wrong kind claim: {claims.get('kind')!r}")
    sub = claims.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("Missing sub claim")
    if not expected_owner_id or sub != expected_owner_id:
        raise jwt.InvalidTokenError("Token sub does not match the platform owner")
    return claims
