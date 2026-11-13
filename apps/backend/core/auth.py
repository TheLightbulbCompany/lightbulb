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
    # Immutable JWT provenance; display/email and tenant ownership are separate.
    verified_issuer: str | None = None

    @property
    def is_org_context(self) -> bool:
        """True when user has active organization selected."""
        return self.org_id is not None

    @property
    def is_org_admin(self) -> bool:
        """True when user has admin role in current org."""
        return self.org_role == "org:admin"


# Shared by resolve_owner_id's 409 and provision_gate's Layer-0 gate.
WORKSPACE_REQUIRED_CODE = "workspace_required"
WORKSPACE_REQUIRED_MESSAGE = "Choose or create a workspace to continue."


def resolve_owner_id(auth: AuthContext) -> str:
    """Return the active organization workspace owner."""
    if not auth.org_id:
        raise HTTPException(
            status_code=409,
            detail={"code": WORKSPACE_REQUIRED_CODE, "message": WORKSPACE_REQUIRED_MESSAGE},
        )
    return auth.org_id


def get_owner_type(auth: AuthContext) -> str:
    """Return the owner type after requiring an active organization."""
    resolve_owner_id(auth)
    return "org"


def is_org_owner_id(owner_id: str) -> bool:
    """Whether an owner_id names a Clerk organization.

    Clerk org IDs are prefixed 'org_'; user IDs are 'user_'. Stable
    convention from Clerk; if either prefix changes upstream, update
    this and the tests together. Use this instead of inlining the
    prefix check so a convention change has one edit point.
    """
    return owner_id.startswith("org_")


def require_org_admin(auth: AuthContext) -> AuthContext:
    """Require an active organization whose current user is an admin."""
    resolve_owner_id(auth)
    if not auth.is_org_admin:
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


def _check_authorized_party(payload: dict) -> None:
    """SEC-8: verify the token's `azp` claim against known frontend origins.

    Clerk session tokens carry NO `aud` claim — `azp` (authorized party, the
    origin that minted the token) is Clerk's documented equivalent check.
    Native-app tokens (mobile) legitimately lack `azp`, so an absent claim
    passes, matching Clerk's own SDK semantics. An empty allowlist disables
    the check (local/CI).

    In audit mode a mismatch is logged + metered but never rejected; enforce
    mode rejects with 401. Flip via CLERK_AZP_MODE once audit is clean.
    """
    azp = payload.get("azp")
    parties = settings.clerk_authorized_parties_list
    if not azp or not parties or azp in parties:
        return
    put_metric("auth.azp.mismatch", dimensions={"mode": settings.CLERK_AZP_MODE})
    logger.warning("JWT azp %r not in CLERK_AUTHORIZED_PARTIES (mode=%s)", azp, settings.CLERK_AZP_MODE)
    if settings.CLERK_AZP_MODE == "enforce":
        raise HTTPException(status_code=401, detail="Invalid token origin")


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
    payload = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        issuer=settings.CLERK_ISSUER,
        leeway=30,  # tolerate 30s clock skew
    )
    _check_authorized_party(payload)
    return payload


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
        payload = await _decode_token(token)
        org = _extract_org_claims(payload)

        ctx = AuthContext(
            user_id=payload["sub"],
            org_id=org["org_id"],
            org_role=org["org_role"],
            org_slug=org["org_slug"],
            org_permissions=org["org_permissions"],
            email=payload.get("email"),
            verified_issuer=payload.get("iss"),
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


HUMAN_SESSION_REQUIRED = "human_session_required"


async def get_human_session(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthContext:
    """`get_current_user`, then only a first-party Clerk SESSION token: for the doors that put a
    person's keystrokes or screen on a Mac (local-session /input, /viewing, /resume, /commands). A session
    token carries `sid`; an OAuth access token issued as a JWT (`typ: at+jwt`, a `client_id`
    claim) and a JWT-template token (no `sid`) are refused with 403. The web, desktop webview
    and mobile call these routes with Clerk's default session token (no template)."""
    ctx = await get_current_user(credentials)
    token = credentials.credentials
    # Already verified above; these reads only classify it.
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    if (
        str(header.get("typ", "")).lower() in ("at+jwt", "application/at+jwt")
        or "client_id" in claims
        or not claims.get("sid")
    ):
        put_metric("auth.human_session.refused")
        logger.warning("AUTH REFUSED: not a first-party session token")
        raise HTTPException(status_code=403, detail=HUMAN_SESSION_REQUIRED)
    return ctx


# Email domains that identify team members, for analytics tagging ONLY
# (run_analytics' internal-owner flag). This is NOT an access check: any
# self-registered Clerk account can carry one of these addresses, so platform
# admin is ``PLATFORM_ADMIN_USER_IDS`` (see ``require_platform_admin``).
_PLATFORM_ADMIN_EMAIL_DOMAINS: tuple[str, ...] = ("@thelightbulb.company",)

# Team members on personal mailboxes, for the same analytics tagging.
_PLATFORM_ADMIN_EXTRA_EMAILS: frozenset[str] = frozenset(
    {
        "prasiddha@gmail.com",
    }
)


def is_internal_email(email: str | None) -> bool:
    """True when *email* looks like a team member's, for run-analytics tagging.
    Grants nothing: never use it to authorize a request."""
    normalized = (email or "").lower()
    return normalized.endswith(_PLATFORM_ADMIN_EMAIL_DOMAINS) or normalized in _PLATFORM_ADMIN_EXTRA_EMAILS


def require_platform_admin(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
    """
    Allow only platform admins (the Lightbulb team) — distinct from customer org admins.

    Gate: the Clerk user id must be in ``PLATFORM_ADMIN_USER_IDS``, a reviewed
    per-env list set by CDK. Not the email claim: anyone can register a Clerk
    account on a team domain in dev, and a retired domain can be re-registered.
    """
    if auth.user_id not in settings.platform_admin_user_ids:
        raise HTTPException(status_code=403, detail="Platform admin access required")
    return auth
