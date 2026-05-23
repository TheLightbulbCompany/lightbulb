"""Unit tests for the paperclip_proxy router (HTTP path).

T14 covers the *unit-testable* surface of the proxy:

* brand-rewrite — pattern correctness + idempotence + non-HTML
  passthrough.
* circuit-breaker — opens above threshold, stays closed below, holds
  open for the cool-down once tripped.
* Set-Cookie domain rewrite — preserves other attributes, adds
  ``Domain=`` if missing, replaces existing value, case-insensitive.
* request-header filtering — drops hop-by-hop + the auth/cookie that
  the client cannot trust to forward.

Full integration tests (FastAPI TestClient + mocked admin client +
mocked PaperclipRepo) are deferred to T18-T20's docker-compose suite,
where the upstream Paperclip server is real and the network path
mirrors production. Mocking the entire request flow at unit level
would mostly verify mocks against mocks.
"""

from __future__ import annotations

import time

import httpx
import pytest

from routers.paperclip_proxy import (
    _BRAND_REWRITES,
    _FAILURE_THRESHOLD_PCT,
    _FAILURE_WINDOW_SECONDS,
    _MIN_REQUESTS_FOR_OPEN,
    _OPEN_STATE_SECONDS,
    _brand_rewrite_html,
    _circuit_open,
    _filter_request_headers,
    _filter_response_headers,
    _record_outcome,
    _recent_5xx,
    _recent_total,
    _rewrite_set_cookie_domain,
)


# ---------------------------------------------------------------
# Brand-rewrite
# ---------------------------------------------------------------


def test_brand_rewrite_replaces_title():
    out = _brand_rewrite_html(b"<html><head><title>Paperclip</title></head></html>")
    assert b"<title>Isol8 Teams</title>" in out
    assert b"<title>Paperclip</title>" not in out


def test_brand_rewrite_replaces_title_case_insensitive():
    out = _brand_rewrite_html(b"<TITLE>PaperClip</TITLE>")
    # Case-insensitive flag should match the tag name regardless of
    # case; replacement is the canonical lowercase form.
    assert b"<title>Isol8 Teams</title>" in out


def test_brand_rewrite_replaces_og_site_name_double_quotes():
    src = b'<meta property="og:site_name" content="Paperclip">'
    out = _brand_rewrite_html(src)
    assert b'content="Isol8"' in out


def test_brand_rewrite_replaces_og_site_name_single_quotes():
    src = b"<meta property='og:site_name' content='Paperclip'>"
    out = _brand_rewrite_html(src)
    assert b"content='Isol8'" in out


def test_brand_rewrite_passes_non_html_unchanged():
    src = b'{"data":"Paperclip"}'
    # JSON happens to contain the brand string, but the patterns are
    # anchored on HTML markup so they don't match — exact passthrough.
    out = _brand_rewrite_html(src)
    assert out == src


def test_brand_rewrite_idempotent():
    src = b"<title>Paperclip</title>"
    once = _brand_rewrite_html(src)
    twice = _brand_rewrite_html(once)
    assert once == twice


def test_brand_rewrite_handles_empty_body():
    assert _brand_rewrite_html(b"") == b""


def test_brand_rewrite_patterns_are_compiled_with_ignorecase():
    # Sanity: the module-level patterns were compiled with the
    # IGNORECASE flag so admin tooling that hand-edits HTML doesn't
    # break the rewrite by varying tag case.
    import re as _re

    for pattern, _ in _BRAND_REWRITES:
        assert pattern.flags & _re.IGNORECASE


# ---------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_breaker_state():
    """Each test gets a clean breaker.

    The module-level ``_recent_*`` deques + ``_circuit_open_until``
    are process-global, so without this every test would inherit the
    counts of the previous one and order would matter.
    """
    import routers.paperclip_proxy as proxy_mod

    _recent_5xx.clear()
    _recent_total.clear()
    proxy_mod._circuit_open_until = 0.0
    yield
    _recent_5xx.clear()
    _recent_total.clear()
    proxy_mod._circuit_open_until = 0.0


def test_circuit_breaker_opens_above_threshold():
    # 7 5xx + 3 200 = 70% failure rate over 10 samples → must open.
    for _ in range(7):
        _record_outcome(503)
    for _ in range(3):
        _record_outcome(200)
    assert _circuit_open() is True


def test_circuit_breaker_stays_closed_below_threshold():
    # 2 5xx + 8 200 = 20% — well below the 50% threshold.
    for _ in range(2):
        _record_outcome(503)
    for _ in range(8):
        _record_outcome(200)
    assert _circuit_open() is False


def test_circuit_breaker_stays_closed_below_min_requests():
    # 100% 5xx but only 5 samples — below the 10-sample minimum,
    # so we don't flap the breaker on a tiny burst.
    for _ in range(5):
        _record_outcome(503)
    assert _circuit_open() is False


def test_circuit_breaker_holds_open_during_cooldown(monkeypatch):
    # Trip the breaker, then assert it stays open even if a single
    # later success would otherwise drop the rate. (Real production
    # traffic during the cool-down is small but nonzero — we don't
    # want to flap.)
    for _ in range(10):
        _record_outcome(503)
    assert _circuit_open() is True

    _record_outcome(200)
    _record_outcome(200)
    assert _circuit_open() is True


def test_circuit_breaker_reopens_path_constants_make_sense():
    # Light defensive check on the constants — tests serve as
    # documentation if anyone changes them.
    assert 0 < _FAILURE_THRESHOLD_PCT <= 1
    assert _FAILURE_WINDOW_SECONDS > 0
    assert _OPEN_STATE_SECONDS > 0
    assert _MIN_REQUESTS_FOR_OPEN >= 1


def test_circuit_breaker_recloses_after_cooldown_window(monkeypatch):
    # Trip the breaker.
    for _ in range(10):
        _record_outcome(503)
    assert _circuit_open() is True

    # Fast-forward time past the cool-down. We monkey-patch
    # ``time.time`` inside the proxy module so the cool-down + the
    # rate-window cutoff both shift.
    import routers.paperclip_proxy as proxy_mod

    fake_now = time.time() + _OPEN_STATE_SECONDS + _FAILURE_WINDOW_SECONDS + 1
    monkeypatch.setattr(proxy_mod.time, "time", lambda: fake_now)

    # All the recorded 5xx samples are now older than the window,
    # so the rate calc returns 0/0 and we skip re-opening.
    assert _circuit_open() is False


# ---------------------------------------------------------------
# Set-Cookie domain rewrite
# ---------------------------------------------------------------


def test_rewrite_set_cookie_replaces_existing_domain():
    src = "session=abc; Path=/; Domain=paperclip.internal; HttpOnly; Secure"
    out = _rewrite_set_cookie_domain(src)
    assert "Domain=.isol8.co" in out
    assert "paperclip.internal" not in out
    # Other attributes preserved.
    assert "Path=/" in out
    assert "HttpOnly" in out
    assert "Secure" in out


def test_rewrite_set_cookie_appends_domain_if_missing():
    src = "session=abc; Path=/; HttpOnly; Secure"
    out = _rewrite_set_cookie_domain(src)
    assert out.endswith("; Domain=.isol8.co")
    # Original attributes are not mutated when appending.
    assert "Path=/" in out
    assert "HttpOnly" in out
    assert "Secure" in out


def test_rewrite_set_cookie_case_insensitive():
    src = "session=abc; domain=other.example; Secure"
    out = _rewrite_set_cookie_domain(src)
    # Existing lowercase ``domain=`` is matched and rewritten — only
    # one occurrence, no duplicate. We preserve the upstream's
    # original attribute-name casing (``\1`` in the regex) since
    # cookies are case-insensitive per RFC 6265 §5.2; browsers honor
    # either form.
    assert out.lower().count("domain=") == 1
    assert ".isol8.co" in out
    assert "other.example" not in out
    # Original case preserved on the attribute name.
    assert "domain=.isol8.co" in out


def test_rewrite_set_cookie_preserves_samesite():
    src = "tok=v; Path=/; SameSite=Lax; HttpOnly"
    out = _rewrite_set_cookie_domain(src)
    assert "SameSite=Lax" in out
    assert "; Domain=.isol8.co" in out


def test_rewrite_set_cookie_custom_target_domain():
    src = "tok=v; Domain=foo"
    out = _rewrite_set_cookie_domain(src, target_domain=".example.com")
    assert "Domain=.example.com" in out
    assert "foo" not in out


# ---------------------------------------------------------------
# Request / response header filtering
# ---------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for starlette.Request that exposes ``.headers``.

    The real Request has loads of state (scope, body, query, etc.)
    but ``_filter_request_headers`` only touches ``headers``, so a
    bare object with a dict-of-dicts is enough for the unit assertion.
    """

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_filter_request_headers_drops_hop_by_hop():
    req = _FakeRequest(
        headers={
            "Host": "company.isol8.co",
            "Connection": "keep-alive",
            "Authorization": "Bearer clerk-token",
            "Cookie": "__session=abc",
            "User-Agent": "test/1.0",
            "Transfer-Encoding": "chunked",
            "X-Forwarded-Host": "company.isol8.co",
        }
    )
    out = _filter_request_headers(req)
    # Stripped:
    assert "Host" not in out and "host" not in out
    assert "Connection" not in out
    assert "Authorization" not in out
    assert "Cookie" not in out
    assert "Transfer-Encoding" not in out
    # Kept:
    assert out.get("User-Agent") == "test/1.0"
    assert out.get("X-Forwarded-Host") == "company.isol8.co"


def test_filter_response_headers_drops_set_cookie_and_content_length():
    # We strip Set-Cookie at this layer because it's re-attached
    # separately after Domain= rewriting; double-attaching would
    # produce two cookies on the wire.
    headers = httpx.Headers(
        [
            ("content-length", "123"),
            ("set-cookie", "session=abc"),
            ("transfer-encoding", "chunked"),
            ("content-type", "text/html"),
        ]
    )
    out = _filter_response_headers(headers)
    assert "content-length" not in {k.lower() for k in out}
    assert "set-cookie" not in {k.lower() for k in out}
    assert "transfer-encoding" not in {k.lower() for k in out}
    assert out.get("content-type") == "text/html"


# ---------------------------------------------------------------
# WebSocket relay — auth gate
# ---------------------------------------------------------------
#
# We unit-test the close-paths only (missing cookie, invalid token,
# missing email claim). The full relay flow needs a real upstream
# WebSocket and is covered by T18-T20's docker-compose suite. Asserting
# anything past auth here would be mocking ``_decode_token`` +
# ``PaperclipRepo`` + ``PaperclipAdminClient`` + ``websockets.connect``
# end-to-end — that's almost entirely "do my mocks return what I told
# them to," which the integration suite proves for real.


def _build_ws_app():
    """Mount the proxy router on a fresh FastAPI app for testing.

    Lazy import so module-level test discovery doesn't pay the cost
    of pulling in heavy router deps.
    """
    from fastapi import FastAPI

    from routers.paperclip_proxy import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_ws_rejects_missing_clerk_cookie():
    """No Clerk session cookie on the upgrade → close before accept().

    Starlette's TestClient surfaces a pre-accept close as
    ``WebSocketDisconnect``. The exact close code rides on the
    exception (``e.code``) — we check it's the auth-failure code we
    documented (4401) so the front-end can distinguish "no auth" from
    "Paperclip not provisioned" (4503).
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = _build_ws_app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/some/live-events/path"):
            pass  # pragma: no cover - we expect to never reach here.

    assert exc_info.value.code == 4401


def test_ws_rejects_invalid_clerk_token(monkeypatch):
    """Invalid JWT in __session cookie → close with 4401 before accept().

    We monkeypatch ``_decode_token`` to raise so we don't depend on
    a real Clerk JWKS fetch in unit tests. The proxy code catches
    *any* exception from the decoder (we don't leak Clerk internals
    to the close reason) and turns it into a 4401 close.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        raise ValueError("not a real jwt")

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_ws_app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/some/live-events/path",
            cookies={"__session": "not.a.real.jwt"},
        ):
            pass  # pragma: no cover

    assert exc_info.value.code == 4401


def test_ws_rejects_missing_email_claim(monkeypatch):
    """Valid JWT but no ``email`` claim → close with 4400.

    This guards the same constraint the HTTP path enforces: Better
    Auth needs an email + password to sign the user in, and the
    Isol8 ``users`` row has no email column to fall back on. If
    Clerk's JWT template ever drops the claim we fail fast at the
    proxy edge instead of producing a confusing upstream auth error.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        # Subject present but email missing — exactly the case we
        # want to surface as 4400 rather than 4401.
        return {"sub": "user_123", "o": {}}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_ws_app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/some/live-events/path",
            cookies={"__session": "valid.but.no.email"},
        ):
            pass  # pragma: no cover

    assert exc_info.value.code == 4400


# ---------------------------------------------------------------
# HTTP relay — auth gate (cookie-or-bearer)
# ---------------------------------------------------------------
#
# The HTTP path serves cross-subdomain browser navigation (the user
# clicking the "Teams" link in /chat → ``https://company.isol8.co``).
# Browser navigation cannot attach an ``Authorization: Bearer`` header,
# so the only credential the gateway sees is the Clerk ``__session``
# cookie that's scoped to ``.isol8.co``. The proxy used to use
# ``Depends(get_current_user)`` (which only reads ``Authorization`` via
# ``HTTPBearer``) and so every browser navigation lands on a 401
# "Not authenticated" JSON page. These tests pin the cookie fallback
# wired in by ``_get_paperclip_user``.
#
# We assert at the auth gate only — past auth, ``proxy()`` calls
# ``PaperclipRepo`` and ``PaperclipAdminClient`` (real DynamoDB +
# Paperclip server). Mocking the full pipeline would mostly verify
# mocks against mocks; the integration suite covers the upstream call.


def _build_http_app():
    """Mount the proxy router on a fresh FastAPI app for testing."""
    from fastapi import FastAPI

    from routers.paperclip_proxy import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_http_rejects_missing_auth_for_api_client():
    """API client (no text/html in Accept) with no auth → JSON 401.

    Distinguishes from browser-nav case which gets bootstrap HTML instead.
    """
    from fastapi.testclient import TestClient

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get("/some/path", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


def test_http_redirects_to_chat_for_browser_nav_no_auth(monkeypatch):
    """Browser nav (Accept: text/html) with no auth → 302 to dev.isol8.co/chat.

    The proxy is no longer an entry point of its own; users start from
    /chat on the Isol8 frontend (Teams button there appends ?__t=jwt).
    Direct navigation to company.isol8.co with no token is a sign of
    a bookmarked URL or stale tab; redirecting them home is friendlier
    than a JSON 401.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://dev.isol8.co/chat?from=teams"


def test_http_accepts_paperclip_session_cookie_and_forwards(monkeypatch):
    """Once the handshake has set the session cookie, subsequent requests
    skip Clerk's JWKS roundtrip and authenticate from the cookie. With no
    Paperclip company row yet (typical first-visit race against the
    background autoprovision task), the proxy returns the auto-refresh
    "setting up" stub instead of a JSON 503.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")
    cookie = proxy_mod._mint_paperclip_session(user_id="user_session_path", email="u@example.com")

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return None  # row missing → setting-up stub

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        cookies={"isol8_paperclip": cookie},
        follow_redirects=False,
    )
    # Past auth (no 401), missing row → 503 setting-up stub with auto-refresh.
    assert resp.status_code == 503
    assert "Setting up your workspace" in resp.text
    assert 'http-equiv="refresh"' in resp.text


def test_http_disabled_company_redirects_to_chat(monkeypatch):
    """Cookie path with company.status='disabled' (cancelled subscription)
    must redirect out to /chat — auto-refresh would loop forever since
    disabled requires admin/billing intervention to clear. Returning the
    auto-refresh stub for ALL non-active states would lock disabled users
    into an 8h refresh loop.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")
    cookie = proxy_mod._mint_paperclip_session(user_id="user_disabled", email="d@example.com")

    class _DisabledCompany:
        status = "disabled"

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return _DisabledCompany()

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        cookies={"isol8_paperclip": cookie},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "from=teams" in resp.headers["location"]
    assert "need=reprovision" in resp.headers["location"]


def test_http_accepts_bearer_header_when_cookie_absent(monkeypatch):
    """Programmatic clients (curl / Postman) authenticate via Bearer header.

    Same downstream behavior as the cookie path: missing row → 302.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    async def _fake_decode(token: str):
        return {"sub": "user_bearer_path", "email": "u@example.com"}

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return None

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        headers={"Authorization": "Bearer valid.bearer.jwt"},
        follow_redirects=False,
    )
    # Bearer auth + missing row → setting-up stub (same as cookie path).
    assert resp.status_code == 503
    assert "Setting up your workspace" in resp.text


def test_http_rejects_invalid_clerk_bearer(monkeypatch):
    """Bearer header with invalid Clerk JWT → 401 Invalid Clerk token."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        raise ValueError("not a real jwt")

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        headers={"Authorization": "Bearer garbage", "Accept": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid Clerk token"}


def test_http_rejects_bearer_missing_subject(monkeypatch):
    """Bearer JWT decodes but lacks sub claim → 401."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {"email": "u@example.com"}  # no sub

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        headers={"Authorization": "Bearer valid.no.sub", "Accept": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Token missing subject"}


def test_http_returns_503_when_jwks_fetch_fails(monkeypatch):
    """JWKS fetch failure on the Bearer path → 503, not 401.

    A transient Clerk/network outage shouldn't be surfaced as a
    credential failure. Mirrors core.auth.get_current_user.
    """
    from fastapi.testclient import TestClient
    import httpx

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        raise httpx.ConnectError("clerk JWKS unreachable")

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path",
        headers={"Authorization": "Bearer any.token", "Accept": "application/json"},
    )
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Authentication service unavailable"}


# ---------------------------------------------------------------
# Handshake endpoint + session cookie helpers
# ---------------------------------------------------------------


def test_decode_clerk_publishable_key_dev():
    """The Clerk Frontend API hostname round-trips out of a dev pub key."""
    from routers.paperclip_proxy import _decode_clerk_publishable_key

    # pk_test_dXAtbW90aC01NS5jbGVyay5hY2NvdW50cy5kZXYk decodes to
    # "up-moth-55.clerk.accounts.dev$" — strip the trailing $.
    pk = "pk_test_dXAtbW90aC01NS5jbGVyay5hY2NvdW50cy5kZXYk"
    assert _decode_clerk_publishable_key(pk) == "up-moth-55.clerk.accounts.dev"


def test_decode_clerk_publishable_key_handles_garbage():
    """Malformed keys return None rather than raising — the bootstrap
    HTML responder uses None as a "render an error page" signal.
    """
    from routers.paperclip_proxy import _decode_clerk_publishable_key

    assert _decode_clerk_publishable_key("") is None
    assert _decode_clerk_publishable_key("not_a_valid_key") is None
    assert _decode_clerk_publishable_key("pk_test_") is None


def test_paperclip_session_jwt_round_trip(monkeypatch):
    """Mint + verify must round-trip with the configured signing key."""
    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    token = proxy_mod._mint_paperclip_session(user_id="u1", email="u1@example.com")
    claims = proxy_mod._verify_paperclip_session(token)
    assert claims["sub"] == "u1"
    assert claims["email"] == "u1@example.com"
    assert claims["kind"] == "paperclip_session"


def test_paperclip_session_rejects_wrong_kind(monkeypatch):
    """Service tokens (kind=paperclip_service) must not be accepted as a
    proxy-session cookie even though they share the same signing key.
    Different ``kind`` claim is the only thing keeping the two segregated.
    """
    import jwt as pyjwt
    import pytest as _pytest
    import routers.paperclip_proxy as proxy_mod
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    # Mint a service-token-shaped JWT (kind="paperclip_service") and try
    # to verify it as a session — must reject.
    now = datetime.now(timezone.utc)
    fake_service_token = pyjwt.encode(
        {
            "sub": "u1",
            "kind": "paperclip_service",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        "test-secret-32-bytes-min-len-x",
        algorithm="HS256",
    )
    with _pytest.raises(pyjwt.InvalidTokenError):
        proxy_mod._verify_paperclip_session(fake_service_token)


def test_handshake_validates_clerk_jwt_and_sets_cookie(monkeypatch):
    """POST /__handshake__ with valid Clerk Bearer → 204 + Set-Cookie."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {"sub": "user_handshake", "email": "h@example.com"}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post(
        "/__handshake__",
        headers={"Authorization": "Bearer valid.clerk.jwt"},
    )
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "isol8_paperclip=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie or "SameSite=Lax" in set_cookie

    # The cookie should round-trip through verify and yield the same sub.
    cookie_val = set_cookie.split("isol8_paperclip=", 1)[1].split(";", 1)[0]
    claims = proxy_mod._verify_paperclip_session(cookie_val)
    assert claims["sub"] == "user_handshake"
    assert claims["email"] == "h@example.com"


def test_handshake_rejects_missing_bearer():
    """No Authorization header on /__handshake__ → 401."""
    from fastapi.testclient import TestClient

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post("/__handshake__")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Missing Clerk JWT"}


def test_handshake_rejects_invalid_clerk_jwt(monkeypatch):
    """/__handshake__ with garbage Bearer → 401."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        raise ValueError("not a real jwt")

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post(
        "/__handshake__",
        headers={"Authorization": "Bearer garbage"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid Clerk token"}


def test_ws_accepts_paperclip_session_cookie(monkeypatch):
    """WS upgrade with isol8_paperclip cookie → past auth gate (closes
    later with 4503 because no PaperclipCompany row, not 4401).

    Mirrors the HTTP path: after the bootstrap establishes the proxy
    session cookie, in-page WebSocket upgrades for live-events use that
    cookie, not Clerk's __session (which never crossed subdomains anyway).
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    cookie = proxy_mod._mint_paperclip_session(user_id="ws_user_session", email="w@example.com")

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return None  # → 4503 not provisioned (proves we passed auth).

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_ws_app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/some/live-events/path",
            cookies={"isol8_paperclip": cookie},
        ):
            pass  # pragma: no cover

    assert exc_info.value.code == 4503  # not provisioned, not 4401


def test_http_handoff_token_via_query_param(monkeypatch):
    """Initial handshake: ?__t=<clerk_jwt> in URL → backend validates,
    container check passes, auto-provisions if needed, mints cookie,
    302s to clean URL. The JWT leaves the address bar via the redirect.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    async def _fake_decode(token: str):
        return {"sub": "user_handoff", "email": "h@example.com"}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    # User has a container.
    async def _fake_get_container(owner_id: str):
        return {"owner_id": owner_id, "status": "running"}

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    # User already has an active Paperclip company → skip autoprovision.
    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            class _Co:
                status = "active"

            return _Co()

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path?other=keep&__t=valid.clerk.jwt",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Token stripped from redirect target; other params preserved.
    assert "__t=" not in resp.headers["location"]
    assert "other=keep" in resp.headers["location"]
    # Session cookie set on the redirect.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "isol8_paperclip=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie


def test_http_handoff_redirects_to_chat_when_no_container(monkeypatch):
    """Initial handshake: user signed in but has no Isol8 container yet.
    Redirect to /chat with need=container hint instead of provisioning a
    Paperclip workspace that would point at a nonexistent OpenClaw.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    async def _fake_decode(token: str):
        return {"sub": "user_no_container", "email": "n@example.com"}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    async def _fake_get_container(owner_id: str):
        return None  # → 302 to /chat

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/?__t=valid.clerk.jwt",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "from=teams" in resp.headers["location"]
    assert "need=container" in resp.headers["location"]
    # No session cookie should be set.
    assert "isol8_paperclip=" not in resp.headers.get("set-cookie", "")
