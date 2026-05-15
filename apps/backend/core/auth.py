import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings
from core.observability.logging import bind_request_context, request_id_var
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)
security = HTTPBearer()
security_optional = HTTPBearer(auto_error=False)

# JWKS cache with TTL — 5 min refresh, 15 min max stale before fail-closed
_jwks_cache: dict = {"data": None, "expires_at": None}
JWKS_CACHE_TTL = timedelta(minutes=5)
_JWKS_MAX_STALE = timedelta(minutes=15)


async def _get_cached_jwks(jwks_url: str) -> dict:
    """Fetch JWKS with TTL-based caching to avoid hitting Clerk on every request."""
    now = datetime.utcnow()

    # Return cached data if still valid
    if _jwks_cache["data"] and _jwks_cache["expires_at"] and now < _jwks_cache["expires_at"]:
        return _jwks_cache["data"]

    # Fetch fresh JWKS
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(jwks_url, timeout=10.0)
            response.raise_for_status()
            jwks = response.json()

        # Update cache
        _jwks_cache["data"] = jwks
        _jwks_cache["expires_at"] = now + JWKS_CACHE_TTL
        put_metric("auth.jwks.refresh", dimensions={"status": "ok"})
        logger.info("JWKS cache refreshed")
        return jwks
    except httpx.HTTPError as e:
        put_metric("auth.jwks.refresh", dimensions={"status": "error"})
        # Serve stale cache up to _JWKS_MAX_STALE, then fail closed
        if _jwks_cache["data"] and _jwks_cache["expires_at"]:
            staleness = now - _jwks_cache["expires_at"]
            if staleness < _JWKS_MAX_STALE:
                logger.warning("JWKS fetch failed, using stale cache (age %s): %s", staleness, e)
                return _jwks_cache["data"]
        logger.error("JWKS fetch failed and cache too stale (or empty): %s", e)
        raise


@dataclass
class AuthContext:
    """Structured auth context from JWT claims.

    Provides convenient properties for checking user context:
    - is_org_context: True when user has active organization selected
    - is_personal_context: True when user is in personal mode
    - is_org_admin: True when user has admin role in current org
    """

    user_id: str
    org_id: str | None = None
    org_role: str | None = None
    org_slug: str | None = None
    org_permissions: list[str] = field(default_factory=list)
    # Caller's primary email, populated from the Clerk JWT `email` claim.
    # Requires the Clerk JWT template to include
    # `"email": "{{user.primary_email_address}}"`. Optional — older tokens
    # issued before that template change won't have it. Used by billing to
    # attach the email to newly-created Stripe customers so the dashboard
    # surfaces who each customer belongs to (including bail-outs who clicked
    # Subscribe but never completed Checkout).
    email: str | None = None

    @property
    def is_org_context(self) -> bool:
        """True when user has active organization selected."""
        return self.org_id is not None

    @property
    def is_personal_context(self) -> bool:
        """True when user is in personal mode (no active org)."""
        return self.org_id is None

    @property
    def is_org_admin(self) -> bool:
        """True when user has admin role in current org."""
        return self.org_role == "org:admin"


def resolve_owner_id(auth: AuthContext) -> str:
    """Return the container/workspace owner: org_id if in org, else user_id."""
    return auth.org_id if auth.is_org_context else auth.user_id


def get_owner_type(auth: AuthContext) -> str:
    """Return 'org' or 'personal' based on auth context."""
    return "org" if auth.is_org_context else "personal"


def require_org_admin(auth: AuthContext) -> AuthContext:
    """Raise 403 if user is in an org but not an admin. Personal context passes through."""
    if auth.is_org_context and not auth.is_org_admin:
        put_metric("auth.org_admin.denied")
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return auth


def _find_rsa_key(jwks: dict, kid: str) -> dict | None:
    """Find RSA key in JWKS by key ID."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key["use"],
                "n": key["n"],
                "e": key["e"],
            }
    return None


async def _decode_token(token: str) -> dict:
    """Fetch JWKS, find the matching RSA key, and decode the JWT.

    Raises jwt/httpx exceptions on failure -- callers handle error mapping.
    """
    jwks_url = f"{settings.CLERK_ISSUER}/.well-known/jwks.json"
    jwks = await _get_cached_jwks(jwks_url)

    unverified_header = jwt.get_unverified_header(token)
    rsa_key = _find_rsa_key(jwks, unverified_header["kid"])
    if not rsa_key:
        raise HTTPException(status_code=401, detail="Invalid token headers")

    public_key = jwt.PyJWK(rsa_key).key
    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=settings.CLERK_AUDIENCE,
        issuer=settings.CLERK_ISSUER,
        leeway=30,  # tolerate 30s clock skew
    )


def _extract_org_claims(payload: dict) -> dict:
    """Extract Clerk v2 organization claims from JWT payload."""
    org_claims = payload.get("o", {})
    org_id = org_claims.get("id")
    org_role_raw = org_claims.get("rol")
    org_slug = org_claims.get("slg")
    org_perms_raw = org_claims.get("per", "")

    return {
        "org_id": org_id,
        "org_role": f"org:{org_role_raw}" if org_role_raw else None,
        "org_slug": org_slug,
        "org_permissions": [p for p in org_perms_raw.split(",") if p] if org_perms_raw else [],
    }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthContext:
    """Validate JWT and return AuthContext with user and org claims."""
    try:
        token = credentials.credentials
        # Diagnostic: log unverified claims to trace auth failures (DEBUG only)
        try:
            unverified = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
            logger.debug(
                "JWT claims: iss=%s sub=%s aud=%s", unverified.get("iss"), unverified.get("sub"), unverified.get("aud")
            )
        except Exception as parse_err:
            logger.debug("Could not decode unverified JWT claims: %s", parse_err)
        payload = await _decode_token(token)
        org = _extract_org_claims(payload)

        ctx = AuthContext(
            user_id=payload["sub"],
            org_id=org["org_id"],
            org_role=org["org_role"],
            org_slug=org["org_slug"],
            org_permissions=org["org_permissions"],
            email=payload.get("email"),
        )
        bind_request_context(request_id_var.get() or "", payload["sub"])
        return ctx

    except jwt.ExpiredSignatureError:
        put_metric("auth.jwt.fail", dimensions={"reason": "expired"})
        logger.warning("AUTH FAIL: JWT expired")
        raise HTTPException(status_code=401, detail="Token expired")
    except (jwt.InvalidAudienceError, jwt.InvalidIssuerError, jwt.MissingRequiredClaimError) as e:
        put_metric("auth.jwt.fail", dimensions={"reason": "claims"})
        logger.warning("AUTH FAIL: JWT claims error: %s", e)
        raise HTTPException(status_code=401, detail="Invalid claims")
    except httpx.HTTPError as e:
        put_metric("auth.jwt.fail", dimensions={"reason": "jwks_unavailable"})
        logger.error(f"Failed to fetch JWKS: {e}")
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except HTTPException:
        raise
    except Exception as e:
        put_metric("auth.jwt.fail", dimensions={"reason": "unknown"})
        logger.error(f"JWT validation error: {e}")
        raise HTTPException(status_code=401, detail="Could not validate credentials")


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_optional),
) -> AuthContext | None:
    """Like get_current_user but returns None instead of raising on missing/invalid auth.

    Use for endpoints that work for both authenticated and anonymous users.
    """
    if credentials is None:
        return None

    try:
        payload = await _decode_token(credentials.credentials)
        org = _extract_org_claims(payload)
        return AuthContext(
            user_id=payload["sub"],
            org_id=org["org_id"],
            org_role=org["org_role"],
            org_slug=org["org_slug"],
            org_permissions=org["org_permissions"],
            email=payload.get("email"),
        )
    except Exception:
        return None


# Email domain that identifies Isol8 team members. Any authenticated user whose
# Clerk JWT carries an email ending with this suffix is a platform admin. No
# env var, no Secrets Manager entry, no Clerk user IDs in git — Clerk itself
# holds the list of who can sign in with an @isol8.co email, and this gate
# just trusts the JWT claim.
_PLATFORM_ADMIN_EMAIL_DOMAIN = "@isol8.co"

# Hardcoded extra-admin allowlist for individuals without an ``@isol8.co``
# mailbox (e.g. team members using personal email while Workspace is being
# set up). Listed in code so changes are auditable in git history. Applies
# to every environment — adding an entry here grants prod admin too, so
# keep the list short and reviewed.
_PLATFORM_ADMIN_EXTRA_EMAILS: frozenset[str] = frozenset(
    {
        "prasiddha@gmail.com",
    }
)


def require_platform_admin(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
    """
    Allow only platform admins (Isol8 team) — distinct from customer org admins.

    Gate: the Clerk JWT's ``email`` claim must end with ``@isol8.co`` OR
    appear in ``_PLATFORM_ADMIN_EXTRA_EMAILS``. Clerk populates the claim
    from the user's primary verified email. Removing someone from the team
    = deleting their Clerk user (or removing their address from the extra
    list / Clerk's @isol8.co email).
    """
    email = (auth.email or "").lower()
    if not email.endswith(_PLATFORM_ADMIN_EMAIL_DOMAIN) and email not in _PLATFORM_ADMIN_EXTRA_EMAILS:
        raise HTTPException(status_code=403, detail="Platform admin access required")
    return auth
