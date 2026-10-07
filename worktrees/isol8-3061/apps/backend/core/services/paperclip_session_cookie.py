"""Client-side per-user Paperclip session cookie reuse.

Replaces the per-request Paperclip sign-in ("the storm") with a single
browser-held ``.isol8.co`` Better Auth session cookie. **The browser is the
primary cache:** the BFF reads the cookie off the request and forwards it
upstream; it only signs in (mints a fresh cookie + sets it on the response)
on a cold start, on expiry, or on a downstream 401. The cookie lives in the
user's own browser, where session cookies belong (the system-owned admin
credential stays in Secrets Manager; that ownership split is deliberate).

Behind that sits a small per-user in-process backstop (``_ColdMint``) for the
callers the browser cache cannot serve: the cold start before any Set-Cookie
has landed, and every forced re-mint. It holds a minted session for its real
lifetime (``_SESSION_REUSE_SECONDS``, well inside Better Auth's 7-day TTL) and
parks an owner whose sign-in draws a 429. Both matter because Better Auth's
sign-in rate limiter keys on IP and the entire backend is one ECS task = one
IP: without reuse the fleet burns a 3-per-10s budget on credentials it already
held, and without the park one looping owner spends everybody else's share of
it. See the constants for the numbers and the reasoning behind them.

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
import hashlib
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

# Browser-facing scope: shared across every subdomain of the product apex (the
# app host, the api host, and the company/board host are all same-site under it),
# so one cookie serves both the native BFF and the proxy. Derived from
# FRONTEND_URL so a domain move never leaves a cookie scoped to the old apex.
_COOKIE_DOMAIN = f".{settings.root_domain}"

# Mirror Better Auth's 7-day session ceiling for the browser's retention; an
# expired/rotated cookie is handled by the 401 re-mint path, not by TTL.
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

# Confused-deputy binding cookie.
_BIND_COOKIE = "isol8_pc_bind"
_BIND_KIND = "paperclip_bind"

# How long a minted session is reused server-side before we sign in again.
#
# Better Auth issues 7-day sessions (its default; the Paperclip fork overrides
# nothing — ``server/src/auth/better-auth.ts`` passes only ``rateLimit.enabled``
# and no ``session.expiresIn``). The 10s this used to hold threw away a
# week-long credential after ten seconds and re-signed-in constantly for
# nothing. 23h is the margin ``paperclip_admin_session`` already uses for the
# bootstrap admin: comfortably inside the 7-day ceiling even with clock skew, at
# most one re-mint per owner per day, and a backend redeploy starts fresh anyway.
#
# A session revoked upstream before this elapses does NOT have to wait out the
# TTL: the downstream 401 self-heal (``main.py``) calls back with ``force=True``
# and re-mints within the same request.
_SESSION_REUSE_SECONDS = 23 * 3600

# Better Auth rate-limits ``POST /api/auth/sign-in/email`` to 3 requests per 10s
# keyed by IP. The whole backend is one ECS task = one IP, so that budget is
# shared by the entire fleet, and one owner looping (401 -> forced re-mint ->
# 429 -> no cookie -> 401) can starve every other owner's first sign-in.
# Parking the owner who drew the 429 is the bulkhead: the park is per-user, so
# they degrade only themselves instead of re-entering the shared bucket on the
# very next request.
_RATE_LIMIT_PARK_SECONDS = 10.0

# Ceiling on a server-supplied ``Retry-After`` so a bad header cannot park an
# owner for hours.
_MAX_PARK_SECONDS = 300.0


class SessionCookieError(Exception):
    """Raised when a fresh session cookie cannot be obtained."""


class SessionRateLimited(SessionCookieError):
    """The owner is parked because their last sign-in drew a 429.

    Carries ``retry_after`` (seconds) so a caller can report it. Distinct from
    the base error so a caller that wants to treat "come back shortly" as
    transient can, without string-matching.
    """

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after
        # Mirrors PaperclipApiError's contract so the drain's transient check
        # (``getattr(e, "retryable", False)``) classifies this correctly too.
        self.retryable = True


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


def _session_hash(session_cookie_line: str) -> str:
    """SHA-256 of the session-cookie VALUE. The binding carries this so it ties
    to THIS exact session token, not merely to a user id — without it, a caller
    authenticated as user X whose binding says ``sub=X`` could inject a
    different user's ``.session_token`` and have us forward it (confused
    deputy). Hash, not the raw value, so the binding cookie never re-exposes
    the bearer-equivalent session token."""
    eq = session_cookie_line.find("=")
    value = session_cookie_line[eq + 1 :] if eq > 0 else session_cookie_line
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mint_binding(user_id: str, session_cookie_line: str) -> str:
    """HS256 JWT binding a SPECIFIC Paperclip session cookie to an Isol8 user.

    Carries the user id AND a hash of the session-token value, so on reuse we
    can verify the presented ``.session_token`` is the exact one this binding
    was minted for — not just that the binding belongs to the right user.
    """
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": user_id,
            "sth": _session_hash(session_cookie_line),
            "kind": _BIND_KIND,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_SESSION_MAX_AGE_SECONDS)).timestamp()),
        },
        _bind_secret(),
        algorithm="HS256",
    )


def _binding_claims(token: str | None) -> dict | None:
    """Decode + validate a binding cookie; ``None`` if absent/invalid/wrong-kind."""
    if not token:
        return None
    try:
        claims = pyjwt.decode(token, _bind_secret(), algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("kind") != _BIND_KIND:
        return None
    return claims


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
    claims = _binding_claims(cookies.get(_BIND_COOKIE))
    if claims is None:
        return None
    # Trust only when the binding is for THIS user AND for THIS exact session
    # token (sub + sth). The sth check is what actually stops the confused
    # deputy: an injected different-user .session_token won't match the
    # binding's hash even if its sub matches the caller.
    if claims.get("sub") != user_id:
        return None
    if claims.get("sth") != _session_hash(presented):
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
        value=mint_binding(user_id, session_cookie_line),
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


def _park_seconds(exc: BaseException) -> float | None:
    """Seconds to park this owner for, or ``None`` if ``exc`` is not a 429.

    Duck-typed on ``status_code``/``retry_after`` rather than importing
    ``PaperclipApiError`` — this module deliberately depends on nothing but
    config + pyjwt, and the admin client imports a great deal more.
    """
    if getattr(exc, "status_code", None) != 429:
        return None
    try:
        hinted = float(getattr(exc, "retry_after", None))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _RATE_LIMIT_PARK_SECONDS
    return min(max(hinted, 1.0), _MAX_PARK_SECONDS)


class _ColdMint:
    """Per-user lock, session buffer, and rate-limit park.

    One lock per user collapses a burst into a single sign-in; the buffer then
    reuses that session for its real lifetime; the park keeps an owner whose
    sign-in was rate-limited out of the shared IP budget until it recovers.
    Everything is keyed by user id, which is what makes a bad owner's blast
    radius exactly one owner.

    ponytail: the three dicts grow one entry per owner seen and are only
    pruned lazily on access. At fleet size (tens to thousands) that is a few
    hundred KB; if it ever matters, an LRU or a periodic sweep of entries whose
    buffer has expired is the upgrade.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._buf: dict[str, tuple[str, float]] = {}
        self._parked_until: dict[str, float] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _fresh(self, user_id: str, stale_before: float | None) -> str | None:
        entry = self._buf.get(user_id)
        if entry is None:
            return None
        cookie, at = entry
        # monotonic everywhere: these are in-process elapsed checks (stamped in
        # mint(), compared here), so a wall-clock step must not extend or
        # collapse the reuse window.
        if (time.monotonic() - at) >= _SESSION_REUSE_SECONDS:
            self._buf.pop(user_id, None)
            return None
        if stale_before is not None and at <= stale_before:
            # A forced caller decided its cookie was bad at ``stale_before``;
            # anything minted at or before that moment is the same bad cookie.
            # Deliberately NOT popped: if the replacement mint then fails (a
            # 429), a later unforced caller can still try what we have rather
            # than being left with nothing.
            return None
        return cookie

    def _parked_for(self, user_id: str) -> float | None:
        until = self._parked_until.get(user_id)
        if until is None:
            return None
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._parked_until.pop(user_id, None)
            return None
        return remaining

    async def mint(self, user_id: str, mint: MintFn, *, stale_before: float | None = None) -> str:
        async with self._lock(user_id):
            # Re-checked under the lock, so a burst that queued behind one
            # sign-in picks up its result instead of each doing their own.
            cached = self._fresh(user_id, stale_before)
            if cached is not None:
                return cached

            remaining = self._parked_for(user_id)
            if remaining is not None:
                raise SessionRateLimited(
                    f"paperclip sign-in for {user_id} is rate-limited; retry in {remaining:.0f}s",
                    retry_after=remaining,
                )

            try:
                cookie = await mint()
            except BaseException as exc:
                park = _park_seconds(exc)
                if park is not None:
                    self._parked_until[user_id] = time.monotonic() + park
                    logger.warning(
                        "paperclip sign-in rate-limited for %s; parking that owner for %.0fs",
                        user_id,
                        park,
                    )
                # The buffer is untouched on ANY failure — a mint that could not
                # replace the session must not also destroy it.
                raise

            if not cookie:
                raise SessionCookieError("sign-in returned no session cookie")
            self._buf[user_id] = (cookie, time.monotonic())
            self._parked_until.pop(user_id, None)
            return cookie

    def evict(self, user_id: str) -> None:
        """Forget everything about ``user_id``.

        Production no longer calls this — a forced re-mint marks the old
        session stale rather than deleting it (see ``resolve_session_cookie``),
        precisely so a failed replacement cannot leave an owner with nothing.
        It survives as the reset primitive for tests, which is also why it
        drops the LOCK: the lock is bound to the event loop that created it,
        and each test gets a new one.
        """
        self._buf.pop(user_id, None)
        self._parked_until.pop(user_id, None)
        self._locks.pop(user_id, None)


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
      (coalesced across a burst), seeds the browser via ``response``, and
      returns the fresh line.

    Raises ``SessionRateLimited`` (a ``SessionCookieError``) when this owner is
    parked after a rate-limited sign-in.

    The caller is responsible for stamping ``Origin: PAPERCLIP_PUBLIC_URL`` on
    the upstream request (Better Auth resolves the ``__Secure-`` cookie name
    off the request URL/Origin).
    """
    stale_before: float | None = None
    if not force:
        trusted = is_trusted(cookies, user_id)
        if trusted is not None:
            return trusted
    else:
        # Mark, don't evict. Evicting here dropped the buffered session BEFORE
        # its replacement existed, so (a) a re-mint that then failed left the
        # owner with nothing, and (b) concurrent 401s each evicted the cookie
        # the other had just minted and signed in again — the exact loop that
        # spent the fleet's rate-limit budget. Recording the instant we decided
        # ours was stale keeps both properties: a session minted AFTER this
        # moment is a genuine replacement and gets reused, one from before does
        # not, and a failed mint destroys nothing.
        stale_before = time.monotonic()
    fresh = await _cold.mint(user_id, mint, stale_before=stale_before)
    set_session_on_response(response, fresh, user_id)
    return fresh
