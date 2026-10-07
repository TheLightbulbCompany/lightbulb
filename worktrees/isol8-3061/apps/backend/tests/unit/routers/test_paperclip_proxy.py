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

import gzip
import json
import time

import httpx
import pytest

from core.config import apex_domain, settings, url_host
from routers.paperclip_proxy import (
    _AGENT_ROUTE_RE,
    _BRAND_REWRITES,
    _FAILURE_THRESHOLD_PCT,
    _FAILURE_WINDOW_SECONDS,
    _MIN_REQUESTS_FOR_OPEN,
    _OPEN_STATE_SECONDS,
    _brand_rewrite_html,
    _circuit_open,
    _filter_request_headers,
    _filter_response_headers,
    _recent_5xx,
    _recent_total,
    _record_outcome,
    _redact_agent_secrets_json,
    _rewrite_set_cookie_domain,
)

# The Set-Cookie Domain is derived from settings.FRONTEND_URL, so the expected
# value must derive too. A literal here matched only while the configured domain
# happened to equal it — which stopped being true when the product domain moved.
COOKIE_APEX = apex_domain(url_host(settings.FRONTEND_URL))


# ---------------------------------------------------------------
# Brand-rewrite
# ---------------------------------------------------------------


def test_brand_rewrite_replaces_title():
    out = _brand_rewrite_html(b"<html><head><title>Paperclip</title></head></html>")
    assert b"<title>Lightbulb Teams</title>" in out
    assert b"<title>Paperclip</title>" not in out


def test_brand_rewrite_replaces_title_case_insensitive():
    out = _brand_rewrite_html(b"<TITLE>PaperClip</TITLE>")
    # Case-insensitive flag should match the tag name regardless of
    # case; replacement is the canonical lowercase form.
    assert b"<title>Lightbulb Teams</title>" in out


def test_brand_rewrite_replaces_og_site_name_double_quotes():
    src = b'<meta property="og:site_name" content="Paperclip">'
    out = _brand_rewrite_html(src)
    assert b'content="Lightbulb"' in out


def test_brand_rewrite_replaces_og_site_name_single_quotes():
    src = b"<meta property='og:site_name' content='Paperclip'>"
    out = _brand_rewrite_html(src)
    assert b"content='Lightbulb'" in out


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

    # Fast-forward time past the cool-down. The breaker now measures its
    # windows with a monotonic clock (immune to wall-clock steps), so we
    # monkey-patch ``time.monotonic`` inside the proxy module — the cool-down
    # + the rate-window cutoff both shift.
    import routers.paperclip_proxy as proxy_mod

    fake_now = time.monotonic() + _OPEN_STATE_SECONDS + _FAILURE_WINDOW_SECONDS + 1
    monkeypatch.setattr(proxy_mod.time, "monotonic", lambda: fake_now)

    # All the recorded 5xx samples are now older than the window,
    # so the rate calc returns 0/0 and we skip re-opening.
    assert _circuit_open() is False


# ---------------------------------------------------------------
# Set-Cookie domain rewrite
# ---------------------------------------------------------------


def test_rewrite_set_cookie_replaces_existing_domain():
    src = "session=abc; Path=/; Domain=paperclip.internal; HttpOnly; Secure"
    out = _rewrite_set_cookie_domain(src)
    assert f"Domain=.{COOKIE_APEX}" in out
    assert "paperclip.internal" not in out
    # Other attributes preserved.
    assert "Path=/" in out
    assert "HttpOnly" in out
    assert "Secure" in out


def test_rewrite_set_cookie_appends_domain_if_missing():
    src = "session=abc; Path=/; HttpOnly; Secure"
    out = _rewrite_set_cookie_domain(src)
    assert out.endswith(f"; Domain=.{COOKIE_APEX}")
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
    assert f".{COOKIE_APEX}" in out
    assert "other.example" not in out
    # Original case preserved on the attribute name.
    assert f"domain=.{COOKIE_APEX}" in out


def test_rewrite_set_cookie_preserves_samesite():
    src = "tok=v; Path=/; SameSite=Lax; HttpOnly"
    out = _rewrite_set_cookie_domain(src)
    assert "SameSite=Lax" in out
    assert f"; Domain=.{COOKIE_APEX}" in out


def test_rewrite_set_cookie_custom_target_domain():
    src = "tok=v; Domain=foo"
    out = _rewrite_set_cookie_domain(src, target_domain=".example.com")
    assert "Domain=.example.com" in out
    assert "foo" not in out


def test_rewrite_set_cookie_default_scope_follows_the_frontend_apex(monkeypatch):
    """Browser-visible: the cookie must stay scoped to the apex every product
    host is a subdomain of, whichever domain that currently is."""
    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.example.ai")
    assert "; Domain=.example.ai" in _rewrite_set_cookie_domain("tok=v")
    # Literal on purpose: this line PAIRS with the monkeypatch above it. The
    # test's whole point is that the cookie follows whatever is configured, so
    # here the expectation must be the patched value, not the derived default.
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://isol8.co")
    assert "; Domain=.isol8.co" in _rewrite_set_cookie_domain("tok=v")


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
            ("content-encoding", "gzip"),
            ("set-cookie", "session=abc"),
            ("transfer-encoding", "chunked"),
            ("content-type", "text/html"),
        ]
    )
    out = _filter_response_headers(headers)
    assert "content-length" not in {k.lower() for k in out}
    assert "content-encoding" not in {k.lower() for k in out}
    assert "set-cookie" not in {k.lower() for k in out}
    assert "transfer-encoding" not in {k.lower() for k in out}
    assert out.get("content-type") == "text/html"


def test_http_proxy_drops_encoding_after_httpx_decodes_upstream_gzip(monkeypatch):
    """The browser must not decode the JSON body a second time.

    ``httpx`` decompresses a gzip response before the proxy reads
    ``upstream.content`` but retains the upstream header. This exercises the
    route boundary to keep the body and response headers in agreement.
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.test")
    cookie = proxy_mod._mint_paperclip_session(user_id="user_proxy", email="user@example.com", org_id="org_proxy")

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return SimpleNamespace(
                status="active",
                org_id="org_proxy",
                paperclip_password_encrypted="unused",
            )

    async def _gzip_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(b'{"agents":[]}'),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            request=request,
        )

    real_async_client = httpx.AsyncClient

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 - mirrors httpx constructor
            kwargs["transport"] = httpx.MockTransport(_gzip_response)
            self._client = real_async_client(*args, **kwargs)

        async def __aenter__(self):
            await self._client.__aenter__()
            return self

        async def __aexit__(self, *args) -> None:  # noqa: ANN002 - mirrors context manager
            await self._client.__aexit__(*args)

        async def request(self, *args, **kwargs):  # noqa: ANN002, ANN003 - mirrors httpx client
            return await self._client.request(*args, **kwargs)

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)
    monkeypatch.setattr(proxy_mod, "is_trusted", lambda _cookies, _user_id: "session=trusted")
    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient", _MockAsyncClient)

    app = _build_http_app()
    client = TestClient(app)
    response = client.get("/api/companies/company/agents", cookies={"isol8_paperclip": cookie})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None
    assert response.json() == {"agents": []}


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
    cookie = proxy_mod._mint_paperclip_session(user_id="user_disabled", email="d@example.com", org_id="org_disabled")

    class _DisabledCompany:
        status = "disabled"
        # Workspace rows carry the active org_id — a MATCHED terminal row,
        # which is what the /chat redirect is for (a mismatched one gets
        # the repair stub instead; see the org-scope tests).
        org_id = "org_disabled"

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


def _stub_handshake_repo_and_dispatch(monkeypatch, row, container=True):
    """Stub the /__handshake__ readiness check's container lookup + repo row
    + the dispatch; returns the list the dispatch stub records (user_id,
    org_id) into."""
    import routers.paperclip_proxy as proxy_mod

    async def _fake_get_container(owner_id: str):
        return {"owner_id": owner_id, "status": "running"} if container else None

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return row

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    calls: list = []

    def _fake_ensure_user_ready(auth):
        calls.append((auth.user_id, auth.org_id))

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(proxy_mod, "ensure_user_ready", _fake_ensure_user_ready)
    return calls


def test_handshake_validates_clerk_jwt_and_sets_cookie(monkeypatch):
    """POST /__handshake__ with valid Clerk Bearer → 204 + Set-Cookie, and
    (no Paperclip row yet) the readiness dispatch fires."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {"sub": "user_handshake", "email": "h@example.com", "o": {"id": "org_handshake"}}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    calls = _stub_handshake_repo_and_dispatch(monkeypatch, row=None)

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

    # No row yet → the same canonical readiness dispatch the ?__t= handoff
    # uses fired here too under the active workspace context.
    assert calls == [("user_handshake", "org_handshake")]


def test_handshake_org_context_dispatches_repair_for_mismatched_row(monkeypatch):
    """An org-context /__handshake__ whose per-user row still points at a
    personal/previous-org company must dispatch the org join/repair BEFORE
    minting the org-scoped cookie — otherwise the very next request lands
    on the repair stub (which never re-spawns) with no repair in flight."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {
            "sub": "user_org_member",
            "email": "m@example.com",
            "o": {"id": "org_acme", "rol": "member"},
        }

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    calls = _stub_handshake_repo_and_dispatch(
        monkeypatch, row=SimpleNamespace(status="active", org_id="user_org_member")
    )

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post(
        "/__handshake__",
        headers={"Authorization": "Bearer valid.clerk.jwt"},
    )
    assert resp.status_code == 204

    # The repair was dispatched under the org context...
    assert calls == [("user_org_member", "org_acme")]

    # ...and the minted cookie carries that same org context.
    set_cookie = resp.headers.get("set-cookie", "")
    cookie_val = set_cookie.split("isol8_paperclip=", 1)[1].split(";", 1)[0]
    claims = proxy_mod._verify_paperclip_session(cookie_val)
    assert claims["org_id"] == "org_acme"


def test_handshake_no_container_skips_dispatch(monkeypatch):
    """An owner with NO container must not get a Paperclip company seeded
    from the handshake — the seeded agents would point at a nonexistent
    OpenClaw, and the active row would let the established-session path
    forward into a dead Teams. Mirrors the ?__t= handoff's container
    guard (which redirects; this XHR just declines to seed)."""
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {"sub": "user_handshake", "email": "h@example.com", "o": {"id": "org_handshake"}}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    calls = _stub_handshake_repo_and_dispatch(monkeypatch, row=None, container=False)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post(
        "/__handshake__",
        headers={"Authorization": "Bearer valid.clerk.jwt"},
    )
    # Cookie still minted (the page reload shows the pre-existing
    # unprovisioned-entry stub); no seed dispatched.
    assert resp.status_code == 204
    assert calls == []


def test_handshake_matched_active_row_skips_dispatch(monkeypatch):
    """Control: an already-provisioned row for THIS owner context mints the
    cookie without any dispatch — handshakes must stay cheap for the
    common re-entry case."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    async def _fake_decode(token: str):
        return {"sub": "user_handshake", "email": "h@example.com", "o": {"id": "org_handshake"}}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    calls = _stub_handshake_repo_and_dispatch(monkeypatch, row=SimpleNamespace(status="active", org_id="org_handshake"))

    app = _build_http_app()
    client = TestClient(app)

    resp = client.post(
        "/__handshake__",
        headers={"Authorization": "Bearer valid.clerk.jwt"},
    )
    assert resp.status_code == 204
    assert calls == []


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
        return {"sub": "user_handoff", "email": "h@example.com", "o": {"id": "org_handoff"}}

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
                # Workspace rows carry the active org_id — matching the
                # handoff's org-scoped readiness check.
                org_id = "org_handoff"

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
        return {"sub": "user_no_container", "email": "n@example.com", "o": {"id": "org_no_container"}}

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


def test_http_handoff_autoprovision_routes_through_canonical_trigger(monkeypatch):
    """INV-11: the proxy's handoff no longer has its own second, undocumented
    autoprovision door (the deleted ``_autoprovision`` helper, which called
    ``provision_org`` directly, keyed on ``user_id``). It must dispatch the
    SAME ``owner_id``-keyed ``ensure_user_ready`` that ``GET /container/status``
    uses — exactly one autoprovision trigger family.
    """
    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    async def _fake_decode(token: str):
        return {"sub": "user_trigger_fold", "email": "t@example.com", "o": {"id": "org_trigger_fold"}}

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    async def _fake_get_container(owner_id: str):
        return {"owner_id": owner_id, "status": "running"}

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    # No active Paperclip row yet → the handoff must spawn autoprovision.
    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return None

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    calls = []

    def _fake_ensure_user_ready(auth):
        # Regular (non-async) stub so the call args are recorded
        # synchronously — exactly when the real ``asyncio.create_task(...)``
        # call site evaluates ``ensure_user_ready(auth)`` to produce the
        # coroutine it schedules — independent of whether the background task
        # actually gets a turn on the loop before the test inspects the
        # response.
        calls.append(auth.user_id)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(proxy_mod, "ensure_user_ready", _fake_ensure_user_ready)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path?__t=valid.clerk.jwt",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    # Dispatched through the lazy JOIN for the active workspace; ensure_user_ready
    # never provisions, so a running box only gets the seed reconcile.
    assert calls == ["user_trigger_fold"]


@pytest.mark.parametrize(
    ("row_org_id", "expect_dispatch"),
    [
        # Active row still pointing at the member's PERSONAL company (or a
        # previous org's) — "active" is true, but not for THIS owner
        # context, so the handoff must still dispatch the canonical trigger
        # to join/repair; suppressing it would sign the member into the
        # stale workspace with nothing on this path ever fixing the row.
        ("user_org_member", True),
        # Active row already pointing at THIS org — genuinely provisioned;
        # no dispatch (the pre-existing fast path).
        ("org_acme", False),
    ],
)
def test_http_handoff_active_row_readiness_is_org_scoped(monkeypatch, row_org_id, expect_dispatch):
    """The handoff's "already provisioned" check must use the same
    org-scoped predicate as provision_org's idempotency short-circuit
    (active AND org_id == resolved owner), not status alone."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    async def _fake_decode(token: str):
        # Org-context Clerk JWT: resolve_owner_id(auth) → "org_acme".
        return {
            "sub": "user_org_member",
            "email": "m@example.com",
            "o": {"id": "org_acme", "rol": "member"},
        }

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    async def _fake_get_container(owner_id: str):
        # The org's shared container exists (keyed on org_id).
        assert owner_id == "org_acme"
        return {"owner_id": owner_id, "status": "running"}

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return SimpleNamespace(status="active", org_id=row_org_id)

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    calls = []

    def _fake_ensure_user_ready(auth):
        calls.append((auth.user_id, auth.org_id))

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(proxy_mod, "ensure_user_ready", _fake_ensure_user_ready)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path?__t=valid.clerk.jwt",
        follow_redirects=False,
    )
    # Both cases still hand off — the cookie is minted regardless; only the
    # repair dispatch is conditional.
    assert resp.status_code == 302

    if expect_dispatch:
        assert calls == [("user_org_member", "org_acme")]
    else:
        assert calls == []


def test_http_handoff_token_wins_over_stale_session_cookie(monkeypatch):
    """A ?__t= handoff token must drive the auth context even when the
    browser still holds a valid session cookie from a previous visit. The
    cookie carries no org claims, so letting it win would run the container
    guard, the org-scoped readiness check, and the autoprovision dispatch
    under the stale personal identity — the exact context switch (personal
    Teams visit → org handoff) the handshake exists to serve."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    # A VALID cookie from a previous personal-context visit.
    stale_cookie = proxy_mod._mint_paperclip_session(user_id="user_org_member", email="m@example.com")

    async def _fake_decode(token: str):
        # The fresh handoff JWT is org-context.
        return {
            "sub": "user_org_member",
            "email": "m@example.com",
            "o": {"id": "org_acme", "rol": "member"},
        }

    monkeypatch.setattr(proxy_mod, "_decode_token", _fake_decode)

    container_lookups = []

    async def _fake_get_container(owner_id: str):
        container_lookups.append(owner_id)
        return {"owner_id": owner_id, "status": "running"}

    monkeypatch.setattr(proxy_mod.container_repo, "get_by_owner_id", _fake_get_container)

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            # Active PERSONAL row — must not suppress the org dispatch.
            return SimpleNamespace(status="active", org_id="user_org_member")

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    calls = []

    def _fake_ensure_user_ready(auth):
        calls.append((auth.user_id, auth.org_id))

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(proxy_mod, "ensure_user_ready", _fake_ensure_user_ready)

    app = _build_http_app()
    client = TestClient(app)

    resp = client.get(
        "/some/path?__t=valid.clerk.jwt",
        cookies={"isol8_paperclip": stale_cookie},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    # The handshake ran under the TOKEN's org context, not the cookie's
    # personal one: container looked up by org_id, and the org-scoped
    # readiness check dispatched the repair.
    assert container_lookups == ["org_acme"]
    assert calls == [("user_org_member", "org_acme")]

    # And the freshly minted cookie PRESERVES the org context, so the
    # established-session requests that follow keep resolving the org
    # owner (see the org-scoped stub tests below).
    import re as _re

    cookie_match = _re.search(r"isol8_paperclip=([^;]+)", resp.headers.get("set-cookie", ""))
    assert cookie_match
    claims = proxy_mod._verify_paperclip_session(cookie_match.group(1))
    assert claims["org_id"] == "org_acme"


def _org_cookie_session_env(monkeypatch, row_org_id, row_status="active"):
    """Shared setup for the established-session org-scope tests: a session
    cookie carrying org context + a per-user row with ``row_status``
    pointing at ``row_org_id``."""
    from types import SimpleNamespace

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    cookie = proxy_mod._mint_paperclip_session(user_id="user_org_member", email="m@example.com", org_id="org_acme")

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return SimpleNamespace(
                status=row_status,
                org_id=row_org_id,
                paperclip_password_encrypted="irrelevant",
            )

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)
    return proxy_mod, cookie


def test_established_session_stubs_on_org_mismatched_active_row(monkeypatch):
    """An org-context session whose per-user row is still active for a
    DIFFERENT owner (personal or previous org) must get the setting-up
    stub, not a forward into the stale workspace — the handshake that
    minted this cookie dispatched the join/repair, and the row re-points
    when it lands."""
    from fastapi.testclient import TestClient

    proxy_mod, cookie = _org_cookie_session_env(monkeypatch, row_org_id="user_org_member")

    app = _build_http_app()
    client = TestClient(app)
    resp = client.get("/some/path", cookies={"isol8_paperclip": cookie})

    assert "Setting up your workspace" in resp.text


@pytest.mark.parametrize("row_status", ["failed", "disabled"])
def test_established_session_stubs_on_org_mismatched_terminal_row(monkeypatch, row_status):
    """A terminal (failed/disabled) row recorded for a DIFFERENT owner
    context says nothing about THIS owner's provision — it must read as
    repair-pending (stub), not fall through to the terminal-status
    redirect and abort the Teams handoff."""
    from fastapi.testclient import TestClient

    proxy_mod, cookie = _org_cookie_session_env(monkeypatch, row_org_id="user_org_member", row_status=row_status)

    app = _build_http_app()
    client = TestClient(app)
    resp = client.get("/some/path", cookies={"isol8_paperclip": cookie}, follow_redirects=False)

    assert resp.status_code != 302
    assert "Setting up your workspace" in resp.text


def test_established_session_redirects_on_own_terminal_row(monkeypatch):
    """Control: a failed row recorded for THIS owner context still takes
    the terminal-status redirect to /chat (explicit retry trigger) — the
    mismatch-stub above must not swallow genuinely-failed provisions."""
    from fastapi.testclient import TestClient

    proxy_mod, cookie = _org_cookie_session_env(monkeypatch, row_org_id="org_acme", row_status="failed")

    app = _build_http_app()
    client = TestClient(app)
    resp = client.get("/some/path", cookies={"isol8_paperclip": cookie}, follow_redirects=False)

    assert resp.status_code == 302
    assert "need=reprovision" in resp.headers["location"]


def test_established_session_orgless_cookie_over_org_row_redirects_for_rehandshake(monkeypatch):
    """A session with NO org claims (legacy pre-org_id cookie, or a genuine
    personal session) sitting on a row that points at an ORG must redirect
    out to /chat for a fresh handshake — not spin on the stub (no repair is
    in flight: the personal-context dispatch is correctly fenced from
    adopting org rows) and not forward into the org workspace blind."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import routers.paperclip_proxy as proxy_mod

    monkeypatch.setattr(proxy_mod.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "test-secret-32-bytes-min-len-x")
    monkeypatch.setattr(proxy_mod.settings, "FRONTEND_URL", "https://dev.isol8.co")

    # Legacy-shaped cookie: no org_id claim.
    cookie = proxy_mod._mint_paperclip_session(user_id="user_org_member", email="m@example.com")

    class _FakeRepo:
        def __init__(self, table_name: str) -> None:
            pass

        async def get(self, _user_id: str):
            return SimpleNamespace(status="active", org_id="org_acme")

    monkeypatch.setattr(proxy_mod, "PaperclipRepo", _FakeRepo)

    app = _build_http_app()
    client = TestClient(app)
    resp = client.get("/some/path", cookies={"isol8_paperclip": cookie}, follow_redirects=False)

    assert resp.status_code == 302
    assert "/chat?from=teams" in resp.headers["location"]


def test_established_session_forwards_on_org_matched_active_row(monkeypatch):
    """Control: the same org-context session with a row already pointing at
    THIS org proceeds past the stub into the forward path — proving the
    mismatch stub above isn't suppressing legitimate org sessions."""
    from fastapi.testclient import TestClient

    proxy_mod, cookie = _org_cookie_session_env(monkeypatch, row_org_id="org_acme")

    def _sentinel(cookies, uid):
        raise RuntimeError("forward-path-reached")

    monkeypatch.setattr(proxy_mod, "is_trusted", _sentinel)

    app = _build_http_app()
    client = TestClient(app)
    with pytest.raises(RuntimeError, match="forward-path-reached"):
        client.get("/some/path", cookies={"isol8_paperclip": cookie})


# ---------------------------------------------------------------------------
# Agent-secret redaction — the proxy fronts the native Paperclip agent routes,
# which echo the full record incl. the openclaw_gateway adapterConfig (device
# signing key + gateway token). Those must be stripped before they hit the DOM.
# ---------------------------------------------------------------------------


def test_agent_route_regex_matches_only_agent_path_segments():
    assert _AGENT_ROUTE_RE.search("api/agents")
    assert _AGENT_ROUTE_RE.search("api/agents/abc123")
    assert _AGENT_ROUTE_RE.search("api/companies/co_1/agents")
    assert _AGENT_ROUTE_RE.search("api/companies/co_1/agents/abc")
    # Non-agent routes don't match; "agents" must be a whole path segment.
    assert not _AGENT_ROUTE_RE.search("api/companies/co_1/issues")
    assert not _AGENT_ROUTE_RE.search("api/health")
    assert not _AGENT_ROUTE_RE.search("api/agentschema")


def test_redact_agent_secrets_json_strips_and_roundtrips():
    body = json.dumps([{"id": "a1", "adapterConfig": {"devicePrivateKeyPem": "LEAK"}}]).encode()
    out = _redact_agent_secrets_json(body)
    assert b"LEAK" not in out
    assert b"adapterConfig" not in out
    assert json.loads(out) == [{"id": "a1"}]


def test_redact_agent_secrets_json_passes_through_non_json():
    body = b"<html>not json</html>"
    assert _redact_agent_secrets_json(body) == body
