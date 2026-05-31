"""Unit tests for the client-side Paperclip session-cookie reuse helper.

Covers the cookie contract both surfaces (Teams BFF + board proxy) share:
  - warm path: a bound browser cookie is reused; ``mint`` is never called.
  - cold start: no cookie -> ``mint`` once, both the session + binding
    cookies seeded on the response.
  - parallel cold start: N concurrent resolves -> exactly one ``mint``
    (singleflight coalesces the burst).
  - binding rejection: a session cookie with no / mismatched
    ``isol8_pc_bind`` is treated as cold (confused-deputy guard).
  - force=True: evicts the singleflight buffer and re-mints.
"""

from __future__ import annotations

import asyncio

import pytest

import core.services.paperclip_session_cookie as sc
from core.services.paperclip_session_cookie import (
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
    # The cold-mint coalescer is a module-global singleton with a short
    # result buffer; evict any cross-test residue for the users we touch.
    yield
    for uid in ("u1", "u2"):
        sc._cold.evict(uid)


def _mint_factory(value: str):
    """An awaitable mint that records how many times it was awaited."""
    calls = {"n": 0}

    async def mint() -> str:
        calls["n"] += 1
        return value

    return mint, calls


@pytest.mark.asyncio
async def test_warm_path_reuses_bound_cookie_without_minting():
    mint, calls = _mint_factory("should-not-be-used")
    cookies = {
        _SESSION_NAME: "abc123",
        "isol8_pc_bind": mint_binding("u1"),
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
    # Session cookie is HttpOnly + .isol8.co scoped.
    seeded = resp.cookies[_SESSION_NAME]
    assert seeded["httponly"] is True
    assert seeded["domain"] == ".isol8.co"


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
        "isol8_pc_bind": mint_binding("u2"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint)

    assert result == f"{_SESSION_NAME}=minted"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_force_evicts_and_remints_even_with_bound_cookie():
    mint, calls = _mint_factory(f"{_SESSION_NAME}=re-minted")
    cookies = {
        _SESSION_NAME: "old-value",
        "isol8_pc_bind": mint_binding("u1"),
    }
    resp = _FakeResponse()

    result = await resolve_session_cookie(cookies=cookies, response=resp, user_id="u1", mint=mint, force=True)

    # force bypasses the warm-path reuse and signs in fresh.
    assert result == f"{_SESSION_NAME}=re-minted"
    assert calls["n"] == 1
    assert _SESSION_NAME in resp.cookies
