"""Unit tests for the client-side Paperclip session-cookie reuse helper.

Covers the cookie contract both surfaces (Teams BFF + board proxy) share:
  - warm path: a bound browser cookie is reused; ``mint`` is never called.
  - cold start: no cookie -> ``mint`` once, both the session + binding
    cookies seeded on the response.
  - parallel cold start: N concurrent resolves -> exactly one ``mint``
    (singleflight coalesces the burst).
  - binding rejection: a session cookie with no / mismatched
    ``isol8_pc_bind`` is treated as cold (confused-deputy guard).
  - force=True: bypasses the buffered session and re-mints.
  - session reuse: a minted session is reused for its real (7-day) lifetime
    instead of being discarded seconds later.
  - the 429 bulkhead: the owner who drew the rate limit is parked, and the
    park is scoped to THAT owner — a different owner still signs in.
  - Retry-After is honoured when the upstream sends one.
  - a FAILED forced re-mint does not destroy the session already buffered.
"""

from __future__ import annotations

import asyncio

import pytest

import core.services.paperclip_session_cookie as sc
from core.config import apex_domain, settings as _settings, url_host
from core.services.paperclip_session_cookie import (
    SessionRateLimited,
    mint_binding,
    resolve_session_cookie,
)

_SESSION_NAME = "__Secure-paperclip-default.session_token"


class _FakeResponse:
    def __init__(self):
        self.cookies: dict[str, dict] = {}

    def set_cookie(self, **kwargs):
        # Keyed by name so the last write per cookie wins (mirrors a real
        # response header set).
        self.cookies[kwargs["key"]] = kwargs


@pytest.fixture(autouse=True)
def _bind_key(monkeypatch):
    # The binding cookie is HS256-signed with PAPERCLIP_SERVICE_TOKEN_KEY.
    monkeypatch.setattr(sc.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-bind-secret", raising=False)


@pytest.fixture(autouse=True)
def _clean_singleflight():
    # The mint coalescer is a module-global singleton holding a session buffer
    # and a rate-limit park; clear both before AND after so no test inherits
    # another's parked owner or cached session.
    for uid in ("u1", "u2"):
        sc._cold.evict(uid)
    yield
    for uid in ("u1", "u2"):
        sc._cold.evict(uid)


def _mint_factory(value: str):
    """An awaitable mint that records how many times it was awaited.

    It yields to the event loop, because the real mint is an HTTP sign-in. A
    mint that never awaits would let ``asyncio.gather`` run each caller to
    completion before starting the next — so the "concurrent" tests would not
    actually overlap and would prove nothing about the coalescer.
    """
    calls = {"n": 0}

    async def mint() -> str:
        calls["n"] += 1
        await asyncio.sleep(0)
        return value

    return mint, calls


@pytest.mark.asyncio
async def test_warm_path_reuses_bound_cookie_without_minting():
    mint, calls = _mint_factory("should-not-be-used")
    cookies = {
        _SESSION_NAME: "abc123",
        "isol8_pc_bind": mint_binding("u1", f"{_SESSION_NAME}=abc123"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=abc123"
    assert calls["n"] == 0
    # Nothing reseeded on the warm path — the browser already holds it.
    assert resp.cookies == {}


@pytest.mark.asyncio
async def test_cold_start_mints_once_and_seeds_both_cookies():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=fresh-value")
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies={}, response=resp, user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=fresh-value"
    assert calls["n"] == 1
    # Both the session cookie and the binding cookie were seeded.
    assert _SESSION_NAME in resp.cookies
    assert "isol8_pc_bind" in resp.cookies
    # Session cookie is HttpOnly and scoped to the configured product apex.
    seeded = resp.cookies[_SESSION_NAME]
    assert seeded["httponly"] is True
    assert seeded["domain"] == f".{apex_domain(url_host(_settings.FRONTEND_URL))}"


@pytest.mark.asyncio
async def test_parallel_cold_start_mints_exactly_once():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=shared")

    async def one():
        return await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    results = await asyncio.gather(*[one() for _ in range(8)])

    assert all(r == f"{_SESSION_NAME}=shared" for r in results)
    # Singleflight collapses the burst to a single sign-in.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_unbound_session_cookie_is_treated_as_cold():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=minted")
    # Session cookie present but NO binding cookie -> cannot trust it.
    cookies = {_SESSION_NAME: "stolen-or-stale"}
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=minted"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_mismatched_binding_is_rejected():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=minted")
    # Binding cookie is for a DIFFERENT user -> confused-deputy guard fires.
    cookies = {
        _SESSION_NAME: "u2s-cookie",
        "isol8_pc_bind": mint_binding("u2", f"{_SESSION_NAME}=u2s-cookie"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=minted"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_right_user_wrong_session_token_is_rejected():
    """The binding is for the correct user but was minted for a DIFFERENT
    session token, so its ``sth`` won't match an injected ``.session_token``.
    A caller authenticated as u1 must NOT be able to ride a different token
    just because the binding's ``sub`` matches (confused-deputy guard)."""
    mint, calls = _mint_factory(f"{_SESSION_NAME}=minted")
    cookies = {
        _SESSION_NAME: "injected-different-token",
        "isol8_pc_bind": mint_binding("u1", f"{_SESSION_NAME}=the-real-token"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint)

    # sth mismatch -> not trusted -> falls through to a fresh mint.
    assert result == f"{_SESSION_NAME}=minted"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_force_evicts_and_remints_even_with_bound_cookie():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=re-minted")
    cookies = {
        _SESSION_NAME: "old-value",
        "isol8_pc_bind": mint_binding("u1", f"{_SESSION_NAME}=old-value"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint, force=True)

    # force bypasses the warm-path reuse and signs in fresh.
    assert result == f"{_SESSION_NAME}=re-minted"
    assert calls["n"] == 1
    assert _SESSION_NAME in resp.cookies


# ---------------------------------------------------------------------------
# Session reuse (the core fix: a 7-day credential was being dropped after 10s)
# ---------------------------------------------------------------------------


class _RateLimited(Exception):
    """Stands in for the ``PaperclipApiError`` Better Auth's limiter produces.

    The module duck-types on ``status_code``/``retry_after`` rather than
    importing the admin client, so a local double is the honest test of that
    contract.
    """

    def __init__(self, retry_after=None):
        super().__init__("POST /api/auth/sign-in/email -> 429")
        self.status_code = 429
        self.retry_after = retry_after
        self.retryable = True


@pytest.mark.asyncio
async def test_session_is_reused_across_calls_without_a_second_mint():
    """The buffered session is held for its real lifetime, so sequential
    cold-path calls share ONE sign-in. Before the fix the buffer expired after
    10s and every call after that re-signed-in against a 3-per-10s budget."""
    mint, calls = _mint_factory(f"{_SESSION_NAME}=reused")

    for _ in range(5):
        result = await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
        assert result == f"{_SESSION_NAME}=reused"

    assert calls["n"] == 1
    # And the window is hours, not seconds — a 10s buffer is what caused this.
    assert sc._SESSION_REUSE_SECONDS >= 3600


@pytest.mark.asyncio
async def test_expired_buffer_mints_again(monkeypatch):
    """Reuse is bounded: past the window we do sign in again."""
    mint, calls = _mint_factory(f"{_SESSION_NAME}=v")

    await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert calls["n"] == 1

    # Collapse the reuse window rather than sleeping through it.
    monkeypatch.setattr(sc, "_SESSION_REUSE_SECONDS", 0.0)

    await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# The bulkhead: a 429 parks ONE owner, not the fleet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_owner_is_parked_instead_of_re_entering_the_bucket():
    calls = {"n": 0}

    async def mint():
        calls["n"] += 1
        raise _RateLimited()

    # First attempt reaches upstream and draws the 429.
    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert calls["n"] == 1

    # Second attempt is refused locally — it must NOT spend another slot of the
    # fleet-wide 3-per-10s sign-in budget.
    with pytest.raises(SessionRateLimited) as parked:
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert calls["n"] == 1
    assert parked.value.retry_after > 0
    # Classified transient, so the brief drain doesn't bill it as a failure.
    assert parked.value.retryable is True


@pytest.mark.asyncio
async def test_a_parked_owner_does_not_block_a_different_owner():
    """THE bulkhead property. One owner looping on 429 must degrade only
    themselves — this is what turned one owner's loop into a platform-wide
    onboarding outage."""
    calls = {"u1": 0, "u2": 0}

    async def mint_u1():
        calls["u1"] += 1
        raise _RateLimited()

    async def mint_u2():
        calls["u2"] += 1
        return f"{_SESSION_NAME}=u2-session"

    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint_u1)
    # u1 is now parked.
    with pytest.raises(SessionRateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint_u1)

    # u2 — a different owner, e.g. someone signing up right now — is unaffected.
    result = await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u2", mint=mint_u2)
    assert result == f"{_SESSION_NAME}=u2-session"
    assert calls["u2"] == 1
    assert calls["u1"] == 1


@pytest.mark.asyncio
async def test_retry_after_from_the_server_is_honoured():
    async def mint():
        # Better Auth answers a 429 with X-Retry-After (seconds); the admin
        # client surfaces it as PaperclipApiError.retry_after.
        raise _RateLimited(retry_after=42)

    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    with pytest.raises(SessionRateLimited) as parked:
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    # Honoured, not replaced by our own default.
    assert 40 < parked.value.retry_after <= 42
    assert sc._RATE_LIMIT_PARK_SECONDS != 42  # i.e. it really came from the server


@pytest.mark.asyncio
async def test_absurd_retry_after_is_clamped():
    async def mint():
        raise _RateLimited(retry_after=999_999)

    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    with pytest.raises(SessionRateLimited) as parked:
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    assert parked.value.retry_after <= sc._MAX_PARK_SECONDS


@pytest.mark.asyncio
async def test_a_successful_mint_clears_the_park():
    state = {"fail": True, "n": 0}

    async def mint():
        state["n"] += 1
        if state["fail"]:
            raise _RateLimited(retry_after=1)
        return f"{_SESSION_NAME}=recovered"

    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    # Park expires; the next attempt gets through and clears it.
    sc._cold._parked_until["u1"] = 0.0
    state["fail"] = False
    result = await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=recovered"
    assert "u1" not in sc._cold._parked_until


# ---------------------------------------------------------------------------
# The eviction race: force must not destroy a good session before replacing it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_forced_remint_keeps_the_buffered_session():
    """``force`` used to evict BEFORE minting, so a re-mint that then failed
    (a 429 — the common case) left the owner with nothing at all."""
    good = f"{_SESSION_NAME}=good"
    state = {"fail": False}

    async def mint():
        if state["fail"]:
            raise _RateLimited()
        return good

    # Seed the buffer with a working session.
    await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert sc._cold._buf["u1"][0] == good

    # A forced re-mint that fails must leave the buffered session intact.
    state["fail"] = True
    with pytest.raises(_RateLimited):
        await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint, force=True)

    assert sc._cold._buf["u1"][0] == good


@pytest.mark.asyncio
async def test_concurrent_forced_remints_sign_in_exactly_once():
    """Concurrent 401s used to each evict the cookie the other had just minted
    and sign in again — the loop that drained the fleet's rate-limit budget.
    They must now share the one replacement."""
    mint, calls = _mint_factory(f"{_SESSION_NAME}=replacement")
    cookies = {
        _SESSION_NAME: "stale",
        "isol8_pc_bind": mint_binding("u1", f"{_SESSION_NAME}=stale"),
    }

    async def one():
        return await resolve_session_cookie(
            cookies=cookies, response=_FakeResponse(), user_id="u1", mint=mint, force=True
        )

    results = await asyncio.gather(*[one() for _ in range(6)])

    assert all(r == f"{_SESSION_NAME}=replacement" for r in results)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_force_still_rejects_the_session_minted_before_the_401():
    """The flip side: ``force`` must not hand back the very session the caller
    just proved was dead, or the 401 self-heal would loop forever."""
    values = iter([f"{_SESSION_NAME}=first", f"{_SESSION_NAME}=second"])

    async def mint():
        return next(values)

    first = await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint)
    assert first == f"{_SESSION_NAME}=first"

    forced = await resolve_session_cookie(cookies={}, response=_FakeResponse(), user_id="u1", mint=mint, force=True)
    assert forced == f"{_SESSION_NAME}=second"
