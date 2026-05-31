"""Client-side per-user Paperclip session cookie reuse.

Replaces the per-request Paperclip sign-in ("the storm") with a single
browser-held ``.isol8.co`` Better Auth session cookie. **The browser is the
cache:** the BFF reads the cookie off the request and forwards it upstream;
it only signs in (mints a fresh cookie + sets it on the response) on a cold
start, on expiry, or on a downstream 401. No per-user server-side store, no
new infra — the cookie lives in the user's own browser, where session
cookies belong (the system-owned admin credential stays in Secrets Manager;
that ownership split is deliberate).

Two surfaces share this module:
  - the native Teams BFF (``routers/teams/*``), which proves the caller's
    Isol8 identity with a Clerk bearer JWT, and
  - the ``company.isol8.co`` proxy (``routers/paperclip_proxy.py``), which
    proves it with the ``isol8_paperclip`` cookie.

Confused-deputy binding (why a presented cookie can't be ridden by another
user): the Paperclip session cookie is opaque — we can't read whose it is.
So whenever we mint it for user X we also set an HS256 *binding* cookie
(``isol8_pc_bind``), signed with the same ``PAPERCLIP_SERVICE_TOKEN_KEY`` and
``kind`` discipline as ``isol8_paperclip``. A presented session cookie is
only trusted + forwarded when its binding cookie's ``sub`` matches the
request's already-authenticated Isol8 user; otherwise we mint fresh for the
authenticated user. Isol8 is the binding issuer — Clerk does nothing special
— which matches the fact that Isol8 owns the Clerk -> Paperclip mapping.

The session cookie stays ``HttpOnly`` (browser auto-sends it, JS cannot read
it -> no XSS exfiltration). "Client-side" means the browser cookie jar, not a
JS-readable token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import jwt as pyjwt

from core.config import settings

logger = logging.getLogger(__name__)

# Better Auth names the session cookie ``[__Secure-]paperclip-<instance>.session_token``.
# Match on the stable suffix rather than the env-dependent prefix.
_SESSION_TOKEN_SUFFIX = ".session_token"

# Browser-facing scope: shared across every *.isol8.co surface (the app host,
# the api host, and the company/board host are all same-site under isol8.co),
# so one cookie serves both the native BFF and the proxy.
_COOKIE_DOMAIN = ".isol8.co"

# Mirror Better Auth's 7-day session ceiling for the browser's retention; an
# expired/rotated cookie is handled by the 401 re-mint path, not by TTL.
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

# Confused-deputy binding cookie.
_BIND_COOKIE = "isol8_pc_bind"
_BIND_KIND = "paperclip_bind"

# Cold-start singleflight: when the Teams page first loads it fires ~6-8 panel
# requests in parallel before any Set-Cookie has landed. Without coalescing
# they would each sign in. A per-user lock + a few-seconds result buffer makes
# the burst share ONE sign-in. This is a coalescer for the cold mint, NOT a
# persistent cache (the browser is the cache); it self-evicts fast.
_COLD_MINT_BUFFER_SECONDS = 10.0


class SessionCookieError(Exception):
    """Raised when a fresh session cookie cannot be obtained."""


# A bare ``name=value`` session-cookie line wrapped with the bookkeeping the
# response setter needs.
MintFn = Callable[[], Awaitable[str]]


# ---------------------------------------------------------------------------
# Binding cookie (HS256, same secret + kind discipline as isol8_paperclip)
# ---------------------------------------------------------------------------


def _bind_secret() -> str:
    key = settings.PAPERCLIP_SERVICE_TOKEN_KEY
    if not key:
        raise SessionCookieError("PAPERCLIP_SERVICE_TOKEN_KEY not configured")
    return key


def mint_binding(user_id: str) -> str:
    """HS256 JWT binding the Paperclip session cookie to an Isol8 user."""
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": user_id,
            "kind": _BIND_KIND,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_SESSION_MAX_AGE_SECONDS)).timestamp()),
        },
        _bind_secret(),
        algorithm="HS256",
    )


def binding_sub(token: str | None) -> str | None:
    """Return the bound user id from a binding cookie, or ``None`` if absent
    or invalid (expired/tampered/wrong-kind)."""
    if not token:
        return None
    try:
        claims = pyjwt.decode(token, _bind_secret(), algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("kind") != _BIND_KIND:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


# ---------------------------------------------------------------------------
# Reading the presented cookies off the request
# ---------------------------------------------------------------------------


def read_session_cookie(cookies: dict[str, str]) -> str | None:
    """Return the bare ``name=value`` Paperclip session-cookie line the
    browser presented, or ``None``. ``cookies`` is ``request.cookies``."""
    for name, value in cookies.items():
        if name.endswith(_SESSION_TOKEN_SUFFIX):
            return f"{name}={value}"
    return None


def is_trusted(cookies: dict[str, str], user_id: str) -> str | None:
    """If the request carries a Paperclip session cookie bound to ``user_id``,
    return the forwardable ``Cookie:`` line; else ``None`` (caller must mint).
    """
    presented = read_session_cookie(cookies)
    if presented is None:
        return None
    if binding_sub(cookies.get(_BIND_COOKIE)) != user_id:
        # Present but unbound / bound to someone else -> never forward it.
        return None
    return presented


# ---------------------------------------------------------------------------
# Setting the cookies on a response
# ---------------------------------------------------------------------------


def set_session_on_response(response, session_cookie_line: str, user_id: str) -> None:
    """Seed the browser with the (domain-broadened, HttpOnly) Paperclip
    session cookie plus its binding cookie. ``session_cookie_line`` is the
    bare ``name=value`` produced by Better Auth sign-in.
    """
    eq = session_cookie_line.find("=")
    if eq <= 0:
        raise SessionCookieError(f"malformed session cookie line: {session_cookie_line!r}")
    name, value = session_cookie_line[:eq], session_cookie_line[eq + 1 :]
    # __Secure- prefixed names REQUIRE Secure=true; dev/prod are https so this
    # always holds. (Local http uses the bare name and is out of scope.)
    response.set_cookie(
        key=name,
        value=value,
        max_age=_SESSION_MAX_AGE_SECONDS,
        domain=_COOKIE_DOMAIN,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.set_cookie(
        key=_BIND_COOKIE,
        value=mint_binding(user_id),
        max_age=_SESSION_MAX_AGE_SECONDS,
        domain=_COOKIE_DOMAIN,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# Cold-start singleflight mint
# ---------------------------------------------------------------------------


class _ColdMint:
    """Per-user lock + short-lived result buffer that collapses a cold-start
    burst into a single sign-in."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._buf: dict[str, tuple[str, float]] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _fresh(self, user_id: str) -> str | None:
        entry = self._buf.get(user_id)
        if entry is None:
            return None
        cookie, at = entry
        if (time.time() - at) >= _COLD_MINT_BUFFER_SECONDS:
            self._buf.pop(user_id, None)
            return None
        return cookie

    async def mint(self, user_id: str, mint: MintFn) -> str:
        async with self._lock(user_id):
            cached = self._fresh(user_id)
            if cached is not None:
                return cached
            cookie = await mint()
            if not cookie:
                raise SessionCookieError("sign-in returned no session cookie")
            self._buf[user_id] = (cookie, time.time())
            return cookie

    def evict(self, user_id: str) -> None:
        self._buf.pop(user_id, None)


_cold = _ColdMint()


# ---------------------------------------------------------------------------
# The entry point used by both surfaces
# ---------------------------------------------------------------------------


async def resolve_session_cookie(
    *,
    cookies: dict[str, str],
    response,
    user_id: str,
    mint: MintFn,
    force: bool = False,
) -> str:
    """Return a forwardable Paperclip ``Cookie:`` line for ``user_id``.

    - Reuses the browser-presented cookie when it is bound to ``user_id``.
    - Otherwise (or when ``force`` after a downstream 401) signs in once
      (coalesced across a cold-start burst), seeds the browser via
      ``response``, and returns the fresh line.

    The caller is responsible for stamping ``Origin: PAPERCLIP_PUBLIC_URL`` on
    the upstream request (Better Auth resolves the ``__Secure-`` cookie name
    off the request URL/Origin).
    """
    if not force:
        trusted = is_trusted(cookies, user_id)
        if trusted is not None:
            return trusted
    else:
        _cold.evict(user_id)
    fresh = await _cold.mint(user_id, mint)
    set_session_on_response(response, fresh, user_id)
    return fresh
