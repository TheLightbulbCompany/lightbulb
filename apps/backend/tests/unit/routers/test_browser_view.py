"""Auth + reachability gating on the browser live-view relay.

The relay is the ONLY door to the container's VNC pipeline, so what these
tests pin is the fence: an upgrade with no/bad credentials must close before
``accept()``, and a valid owner whose container isn't reachable must get the
distinct close code the panel's "asleep" state keys off. The relay loop itself
is byte-forwarding shared with the proven paperclip relay pattern and is
exercised end-to-end in dev, not simulated here.
"""

import pytest


def _build_app():
    from fastapi import FastAPI

    from routers.browser_view import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_webrtc_session_bootstrap_sets_scoped_host_only_cookies(monkeypatch):
    """A Clerk-authenticated bootstrap never exposes the owner or upstream IP.

    This fails if the new endpoint is absent, if it stops deriving the owner
    from the Clerk claims, or if either browser-held credential becomes a
    cross-host/non-HttpOnly cookie.
    """
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(token: str):
        assert token == "clerk.jwt"
        return {"sub": "user_test1"}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            assert owner_id == "user_test1"
            return {"status": "running"}, "10.0.0.42"

    async def _fake_neko_login(ip: str, cookie: str | None):
        assert ip == "10.0.0.42"
        assert cookie is None
        return "neko.session"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "_neko_session", _fake_neko_login, raising=False)

    response = TestClient(_build_app(), base_url="http://localhost:8000").post(
        "/session",
        headers={"Authorization": "Bearer clerk.jwt", "Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "iframe_url": "/api/v1/browser-view/webrtc/?cast=1",
        "state": "ready",
    }
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        assert "Domain=" not in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/api/v1/browser-view" in cookie
        assert "Max-Age=900" in cookie
        assert "Secure" not in cookie


def test_webrtc_session_cookie_is_secure_in_deployed_environment_despite_forged_host(monkeypatch):
    """Only trusted deployment state, never a client Host header, controls Secure."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod
    from core.config import settings

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    async def _fake_neko(ip, cookie):
        return "neko.session"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod, "_neko_session", _fake_neko)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    response = TestClient(_build_app()).post(
        "/session",
        headers={"Authorization": "Bearer clerk.jwt", "Origin": "http://localhost:3000", "Host": "localhost"},
    )

    assert response.status_code == 200
    assert all("Secure" in cookie for cookie in response.headers.get_list("set-cookie"))


@pytest.mark.parametrize("token", [None, "invalid.jwt", "expired.jwt"])
def test_webrtc_session_rejects_missing_invalid_or_expired_auth(monkeypatch, token):
    """Bootstrap accepts only a successfully decoded Clerk bearer token."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(value):
        raise ValueError(f"invalid token: {value}")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    headers = {"Origin": "http://localhost:3000"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = TestClient(_build_app()).post("/session", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("origin", [None, "https://attacker.example"])
def test_webrtc_session_rejects_missing_or_cross_site_origin_before_auth(monkeypatch, origin):
    """Bootstrap is Origin-gated before its bearer credential is inspected."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected(*args):
        raise AssertionError("origin rejection must precede auth and Neko login")

    monkeypatch.setattr(mod, "_browser_auth", _unexpected)
    headers = {} if origin is None else {"Origin": origin}
    response = TestClient(_build_app()).post("/session", headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize("route", ["/session", "/webrtc/api/whoami"])
def test_webrtc_http_routes_report_unreachable_before_neko_io(monkeypatch, route):
    """Both bootstrap and proxy use the distinct runtime-unreachable response."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _unreachable(auth):
        raise HTTPException(status_code=503)

    async def _unexpected(*args):
        raise AssertionError("unreachable owner must not contact Neko")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _unreachable)
    monkeypatch.setattr(mod, "_neko_session", _unexpected)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _unexpected)
    if route == "/session":
        response = TestClient(_build_app()).post(
            route, headers={"Authorization": "Bearer clerk.jwt", "Origin": "http://localhost:3000"}
        )
    else:
        response = TestClient(_build_app()).get(
            route, cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"}
        )

    assert response.status_code == 503


def test_webrtc_session_org_member_resolves_org_owner(monkeypatch):
    """New browser-view sessions use the same org owner resolver as VNC."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(token):
        return {"sub": "user_member", "o": {"id": "org_acme", "rol": "admin", "slg": "acme"}}

    seen: dict[str, str] = {}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            seen["owner_id"] = owner_id
            return {"status": "running"}, "10.0.0.42"

    async def _fake_neko(ip, cookie):
        return "neko.session"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "_neko_session", _fake_neko)
    response = TestClient(_build_app()).post(
        "/session",
        headers={"Authorization": "Bearer clerk.jwt", "Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    assert seen["owner_id"] == "org_acme"


def test_webrtc_broadcast_descendants_reject_before_upstream(monkeypatch):
    """Neko's admin broadcast route stays unreachable through every spelling."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    upstream_called = False

    async def _fake_decode(token: str):
        return {"sub": "user_test1"}

    async def _unexpected_resolve(*args):
        nonlocal upstream_called
        upstream_called = True
        raise AssertionError("broadcast must reject before resolving an upstream")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _unexpected_resolve, raising=False)

    response = TestClient(_build_app()).get(
        "/webrtc/api/room/%62roadcast/child",
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
    )

    assert response.status_code == 403
    assert not upstream_called


@pytest.mark.parametrize(
    "path",
    [
        "api/batch",
        "api/batch/child",
        "api/room/%62roadcast",
        "api/room/%2e%2e/room/broadcast",
        r"api\\room\\broadcast",
        "api%252froom%252fbroadcast",
        "api%2525252525252froom%2525252525252fbroadcast",
    ],
)
def test_webrtc_admin_routes_reject_normalized_variants(path):
    """No spelling of Neko's admin routes reaches the owner process."""
    from fastapi import HTTPException

    import routers.browser_view as mod

    with pytest.raises(HTTPException) as exc_info:
        mod._upstream_path(path)

    assert exc_info.value.status_code == 403


def test_webrtc_path_decode_is_bounded_and_fails_closed(monkeypatch):
    """Nested encoding cannot turn path normalization into unbounded CPU work."""
    from urllib.parse import quote

    from fastapi import HTTPException

    import routers.browser_view as mod

    encoded = "api/room/broadcast"
    for _ in range(mod._MAX_PATH_DECODE_PASSES + 1):
        encoded = quote(encoded, safe="")
    original_unquote = mod.unquote
    calls = 0

    def _counted_unquote(value):
        nonlocal calls
        calls += 1
        return original_unquote(value)

    monkeypatch.setattr(mod, "unquote", _counted_unquote)
    with pytest.raises(HTTPException) as exc_info:
        mod._upstream_path(encoded)

    assert exc_info.value.status_code == 403
    assert calls == mod._MAX_PATH_DECODE_PASSES + 1


def test_webrtc_batch_rejects_before_auth_or_upstream(monkeypatch):
    """Neko's batch dispatcher cannot smuggle an admin broadcast subrequest."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected(*args):
        raise AssertionError("batch must reject before auth, resolve, or upstream I/O")

    monkeypatch.setattr(mod, "_browser_auth", _unexpected)
    response = TestClient(_build_app()).post(
        "/webrtc/api/batch",
        json=[{"path": "/api/room/broadcast/start", "method": "POST"}],
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "api/room/%2e%2e/room/broadcast",
        r"api\\room\\broadcast",
        "api%2525252525252froom%2525252525252fbroadcast",
        "api/x/%2e%2e/batch",
        r"api\\batch",
        "api%2525252525252fbatch",
    ],
)
def test_webrtc_normalized_admin_paths_reject_at_route_before_all_boundaries(monkeypatch, path):
    """Dot segments, slashes, and backslashes cannot evade the route fence."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected(*args):
        raise AssertionError("blocked route must not reach auth, owner lookup, or Neko")

    monkeypatch.setattr(mod, "_browser_auth", _unexpected)
    monkeypatch.setattr(mod, "_resolve_running", _unexpected)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _unexpected)
    response = TestClient(_build_app()).get(f"/webrtc/{path}")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "api/room/broadcast%3Fignored",
        "api/room/broadcast%253Fignored",
        "api/batch%23ignored",
        "api/batch%2523ignored",
    ],
)
def test_webrtc_encoded_delimiters_reject_before_auth_resolve_or_upstream(monkeypatch, path):
    """Encoded query/fragment delimiters cannot disguise Neko admin paths."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected(*args):
        raise AssertionError("blocked route must not authenticate, resolve, or connect upstream")

    monkeypatch.setattr(mod, "_browser_auth", _unexpected)
    monkeypatch.setattr(mod, "_resolve_running", _unexpected)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _unexpected)
    response = TestClient(_build_app()).get(f"/webrtc/{path}")

    assert response.status_code == 403


def test_webrtc_proxy_uses_only_the_resolved_private_neko_endpoint(monkeypatch):
    """Client host, forwarded headers, and Isol8's cookie never reach Neko."""
    import httpx
    from fastapi.testclient import TestClient

    import routers.browser_view as mod
    from core.config import settings

    seen: dict[str, object] = {}

    async def _fake_decode(token: str):
        assert token == "clerk.jwt"
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        assert auth.user_id == "user_test1"
        return "10.0.0.42"

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"neko"

    class _FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def build_request(self, method, url, **kwargs):
            seen.update(method=method, url=url, headers=kwargs["headers"])
            return httpx.Request(method, url)

        async def send(self, request, stream):
            assert stream is True
            return httpx.Response(
                200,
                stream=_Stream(),
                headers={
                    "content-type": "text/plain",
                    "set-cookie": "NEKO_SESSION=refreshed.session; Path=/",
                    "x-neko-internal": "do-not-forward",
                },
                request=request,
            )

        async def aclose(self):
            seen["closed"] = True

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    response = TestClient(_build_app()).get(
        "/webrtc/api/whoami?cast=1&quality=high%20detail",
        headers={
            "Connection": "keep-alive",
            "Host": "attacker.example",
            "X-Forwarded-For": "198.51.100.8",
            "X-Forwarded-Host": "attacker.example",
            "X-Isol8-Owner": "other-owner",
            "X-Real-IP": "198.51.100.8",
        },
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session", "__session": "never-forward"},
    )

    assert response.status_code == 200
    assert response.content == b"neko"
    assert seen["url"] == "http://10.0.0.42:6081/api/v1/browser-view/webrtc/api/whoami?cast=1&quality=high%20detail"
    assert seen["headers"] == {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "cookie": "NEKO_SESSION=neko.session",
    }
    assert seen["closed"] is True
    assert response.headers.get("x-neko-internal") is None
    refreshed = next(cookie for cookie in response.headers.get_list("set-cookie") if "neko=refreshed.session" in cookie)
    assert "Domain=" not in refreshed
    assert all(
        attribute in refreshed
        for attribute in ("HttpOnly", "SameSite=strict", "Path=/api/v1/browser-view", "Max-Age=900", "Secure")
    )


@pytest.mark.parametrize(
    ("payload", "owner"),
    [
        ({"sub": "user_test1"}, "user_test1"),
        ({"sub": "user_member", "o": {"id": "org_acme", "rol": "admin"}}, "org_acme"),
    ],
)
@pytest.mark.parametrize("origin", ["https://isol8.co", "https://bv.isol8.co"])
def test_webrtc_unsafe_http_route_resolves_personal_or_org_owner(monkeypatch, payload, owner, origin):
    """Actual unsafe proxy routes accept only the production frontend/BV pair."""
    import httpx
    from fastapi.testclient import TestClient

    import routers.browser_view as mod
    from core.config import settings

    seen: dict[str, str] = {}

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

    class _Client:
        def __init__(self, **kwargs):
            pass

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, request, stream):
            return httpx.Response(200, stream=_Stream(), request=request)

        async def aclose(self):
            pass

    async def _fake_decode(token):
        return payload

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            seen["owner"] = owner_id
            return {"status": "running"}, "10.0.0.42"

    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://isol8.co")
    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    response = TestClient(_build_app()).post(
        "/webrtc/api/screen",
        headers={"Origin": origin},
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
    )

    assert response.status_code == 200
    assert seen["owner"] == owner


@pytest.mark.parametrize("origin", [None, "https://attacker.example"])
def test_webrtc_unsafe_request_rejects_missing_or_cross_site_origin_before_resolve(monkeypatch, origin):
    """Cookie authentication alone never authorizes a cross-site mutation."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected_resolve(*args):
        raise AssertionError("origin rejection must happen before upstream resolution")

    monkeypatch.setattr(mod, "_resolve_running", _unexpected_resolve)
    headers = {} if origin is None else {"Origin": origin}
    response = TestClient(_build_app()).post(
        "/webrtc/api/screen", headers=headers, cookies={"isol8_browser_view": "bad", "neko": "neko.session"}
    )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://localhost:8000"])
async def test_webrtc_unsafe_request_allows_only_frontend_or_matching_bv_origin(monkeypatch, origin):
    """Unsafe cookie requests accept the configured frontend and its BV host."""
    from starlette.requests import Request

    import routers.browser_view as mod

    async def _fake_auth(token):
        return object()

    monkeypatch.setattr(mod, "_browser_auth", _fake_auth)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/webrtc/api/screen",
            "query_string": b"",
            "headers": [(b"origin", origin.encode()), (b"cookie", b"isol8_browser_view=clerk.jwt; neko=neko.session")],
            "server": ("localhost", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )

    _, neko = await mod._webrtc_auth(request)
    assert neko == "neko.session"


@pytest.mark.parametrize("token", [None, "invalid.jwt", "expired.jwt"])
def test_webrtc_http_rejects_missing_invalid_or_expired_auth(monkeypatch, token):
    """The cookie-authenticated proxy has the same Clerk fence as bootstrap."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(value):
        raise ValueError(f"invalid token: {value}")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    cookies = {"neko": "neko.session"}
    if token:
        cookies["isol8_browser_view"] = token
    response = TestClient(_build_app()).get("/webrtc/api/whoami", cookies=cookies)

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["disconnect", "send_error", "send_error_close_error", "cancel", "upstream_error"])
async def test_webrtc_response_closes_resources_on_actual_asgi_abort(monkeypatch, end):
    """All ASGI aborts and upstream iteration failures close both resources."""
    import asyncio
    import httpx
    from starlette.requests import Request
    from starlette.responses import ClientDisconnect

    import routers.browser_view as mod

    seen: dict[str, bool] = {}

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            if end == "upstream_error":
                raise RuntimeError("upstream stream failed")
            await asyncio.Event().wait()

        async def aclose(self):
            seen["upstream_closed"] = True
            if end == "send_error_close_error":
                raise RuntimeError("upstream close failed")

    class _Client:
        def __init__(self, **kwargs):
            pass

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, request, stream):
            return httpx.Response(200, stream=_Stream(), request=request)

        async def aclose(self):
            seen["client_closed"] = True

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/webrtc/api/whoami",
            "query_string": b"",
            "headers": [(b"cookie", b"isol8_browser_view=clerk.jwt; neko=neko.session")],
            "server": ("localhost", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )
    response = await mod._webrtc_proxy(request, "api/whoami")
    started = asyncio.Event()

    async def receive():
        if end == "disconnect":
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            started.set()
        if end.startswith("send_error") and message["type"] == "http.response.body":
            raise OSError("client disconnected")

    scope = {"type": "http", "asgi": {"spec_version": "2.3" if end == "disconnect" else "2.4"}}
    if end.startswith("send_error"):
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    elif end == "cancel":
        task = asyncio.create_task(response(scope, receive, send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif end == "upstream_error":
        with pytest.raises(RuntimeError, match="upstream stream failed"):
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)
    assert seen == {"upstream_closed": True, "client_closed": True}


@pytest.mark.asyncio
async def test_webrtc_cancelled_upstream_connect_closes_client_and_reraises(monkeypatch):
    """Cancellation while opening Neko is not translated into a 502."""
    import asyncio
    import httpx
    from starlette.requests import Request

    import routers.browser_view as mod

    closed = False

    class _Client:
        def __init__(self, **kwargs):
            pass

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, request, stream):
            raise asyncio.CancelledError

        async def aclose(self):
            nonlocal closed
            closed = True

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/webrtc/api/whoami",
            "query_string": b"",
            "headers": [(b"cookie", b"isol8_browser_view=clerk.jwt; neko=neko.session")],
            "server": ("localhost", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )

    with pytest.raises(asyncio.CancelledError):
        await mod._webrtc_proxy(request, "api/whoami")
    assert closed


@pytest.mark.asyncio
async def test_neko_login_maps_transport_failure_to_502(monkeypatch):
    """A down auxiliary Neko process never leaks an httpx failure to the client."""
    import httpx
    from fastapi import HTTPException

    import routers.browser_view as mod

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: _Client())
    with pytest.raises(HTTPException) as exc_info:
        await mod._neko_session("10.0.0.42", None)

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_neko_session_reuses_valid_upstream_cookie(monkeypatch):
    """A valid browser-held Neko cookie avoids minting a new admin session."""
    import httpx

    import routers.browser_view as mod

    seen: dict[str, object] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            seen.update(url=url, headers=kwargs["headers"])
            return httpx.Response(200, request=httpx.Request("GET", url))

        async def post(self, *args, **kwargs):
            raise AssertionError("valid cookie must not log in again")

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: _Client())

    assert await mod._neko_session("10.0.0.42", "existing.session") == "existing.session"
    assert seen == {
        "url": "http://10.0.0.42:6081/api/v1/browser-view/webrtc/api/whoami",
        "headers": {"Cookie": "NEKO_SESSION=existing.session"},
    }


@pytest.mark.asyncio
async def test_neko_session_rejected_login_maps_to_502(monkeypatch):
    """A Neko login status failure becomes the stable unavailable response."""
    import httpx
    from fastapi import HTTPException

    import routers.browser_view as mod

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: _Client())
    with pytest.raises(HTTPException) as exc_info:
        await mod._neko_session("10.0.0.42", None)

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_cookie", [None, "stale.session"])
async def test_neko_session_mints_after_missing_or_invalid_cookie(monkeypatch, stale_cookie):
    """Missing and rejected cookies both obtain the pinned Neko cookie name."""
    import httpx

    import routers.browser_view as mod

    calls: list[str] = []
    login: dict[str, object] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            calls.append("get")
            return httpx.Response(401, request=httpx.Request("GET", url))

        async def post(self, url, **kwargs):
            calls.append("post")
            login.update(url=url, body=kwargs["json"])
            return httpx.Response(
                200,
                headers={"set-cookie": "NEKO_SESSION=fresh.session; Path=/"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: _Client())

    assert await mod._neko_session("10.0.0.42", stale_cookie) == "fresh.session"
    assert calls == (["post"] if stale_cookie is None else ["get", "post"])
    assert login == {
        "url": "http://10.0.0.42:6081/api/v1/browser-view/webrtc/api/login",
        "body": {"username": "owner"},
    }


def test_webrtc_ws_uses_pinned_neko_api_ws_and_cookie(monkeypatch):
    """Pinned Neko's signaling endpoint accepts only its real session cookie."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    seen: dict[str, object] = {}

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    class _Upstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Connect:
        def __init__(self, url, **kwargs):
            seen.update(url=url, headers=kwargs["additional_headers"])

        async def __aenter__(self):
            return _Upstream()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod, "ws_connect", _Connect)
    with TestClient(_build_app()).websocket_connect(
        "/webrtc/ws",
        headers={"Origin": "http://localhost:8000"},
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
    ):
        pass

    assert seen == {
        "url": "ws://10.0.0.42:6081/api/v1/browser-view/webrtc/api/ws",
        "headers": {"Cookie": "NEKO_SESSION=neko.session"},
    }


@pytest.mark.parametrize(
    ("payload", "owner"),
    [
        ({"sub": "user_test1"}, "user_test1"),
        ({"sub": "user_member", "o": {"id": "org_acme", "rol": "admin"}}, "org_acme"),
    ],
)
@pytest.mark.parametrize("origin", ["https://isol8.co", "https://bv.isol8.co"])
def test_webrtc_ws_route_resolves_personal_or_org_owner(monkeypatch, payload, owner, origin):
    """Actual signaling routes derive both owner types before dialing Neko."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod
    from core.config import settings

    seen: dict[str, str] = {}

    class _Upstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Connect:
        async def __aenter__(self):
            return _Upstream()

        async def __aexit__(self, *args):
            return False

    async def _fake_decode(token):
        return payload

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            seen["owner"] = owner_id
            return {"status": "running"}, "10.0.0.42"

    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://isol8.co")
    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "ws_connect", lambda *args, **kwargs: _Connect())
    with TestClient(_build_app()).websocket_connect(
        "/webrtc/ws", headers={"Origin": origin}, cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"}
    ):
        pass

    assert seen["owner"] == owner


def test_webrtc_ws_viewer_disconnect_cancels_other_leg_and_closes_upstream(monkeypatch):
    """The Neko relay, not just VNC, tears down on first completed leg."""
    import asyncio
    import threading

    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    closed = threading.Event()

    class _Upstream:
        async def send(self, data):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    class _Connect:
        async def __aenter__(self):
            return _Upstream()

        async def __aexit__(self, *args):
            closed.set()
            return False

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod, "ws_connect", lambda *args, **kwargs: _Connect())
    with TestClient(_build_app()).websocket_connect(
        "/webrtc/ws",
        headers={"Origin": "http://localhost:8000"},
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
    ) as ws:
        ws.close()
        assert closed.wait(timeout=5), "Neko signaling socket leaked after viewer disconnect"


def test_webrtc_ws_upstream_completion_closes_viewer(monkeypatch):
    """An upstream Neko completion also ends the viewer-side session."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    class _Upstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Connect:
        async def __aenter__(self):
            return _Upstream()

        async def __aexit__(self, *args):
            return False

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _fake_resolve(auth):
        return "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod, "ws_connect", lambda *args, **kwargs: _Connect())
    with TestClient(_build_app()).websocket_connect(
        "/webrtc/ws",
        headers={"Origin": "http://localhost:8000"},
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
    ) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_webrtc_ws_unreachable_container_closes_4503(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _fake_decode(token):
        return {"sub": "user_test1"}

    async def _unreachable(auth):
        from fastapi import HTTPException

        raise HTTPException(status_code=503)

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _unreachable)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(_build_app()).websocket_connect(
            "/webrtc/ws",
            headers={"Origin": "http://localhost:8000"},
            cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session"},
        ):
            pass

    assert exc_info.value.code == 4503


@pytest.mark.parametrize("token", [None, "invalid.jwt", "expired.jwt"])
def test_webrtc_ws_rejects_missing_invalid_or_expired_auth(monkeypatch, token):
    """Signaling closes before connect when the browser-view credential is bad."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _fake_decode(value):
        raise ValueError(f"invalid token: {value}")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    cookies = {"neko": "neko.session"}
    if token:
        cookies["isol8_browser_view"] = token
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(_build_app()).websocket_connect(
            "/webrtc/ws", headers={"Origin": "http://localhost:8000"}, cookies=cookies
        ):
            pass

    assert exc_info.value.code == 4401


@pytest.mark.parametrize("origin", [None, "https://attacker.example"])
def test_webrtc_ws_rejects_missing_or_cross_site_origin_before_upstream(monkeypatch, origin):
    """All signaling upgrades carry one of the exact allowed origins."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _unexpected(*args):
        raise AssertionError("origin failure must precede auth, resolve, and connect")

    monkeypatch.setattr(mod, "_browser_auth", _unexpected)
    headers = {} if origin is None else {"Origin": origin}
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(_build_app()).websocket_connect("/webrtc/ws", headers=headers):
            pass

    assert exc_info.value.code == 4403


def test_ws_rejects_missing_clerk_cookie():
    """No ``__session`` cookie on the upgrade → 4401 before accept()."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover - never reached

    assert exc_info.value.code == 4401


def test_ws_rejects_invalid_clerk_token(monkeypatch):
    """Undecodable JWT → 4401; decoder internals never leak to the client."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _fake_decode(token: str):
        raise ValueError("not a real jwt")

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", cookies={"__session": "bad.jwt"}):
            pass  # pragma: no cover

    assert exc_info.value.code == 4401


def test_ws_unreachable_container_closes_4503(monkeypatch):
    """Valid owner, container asleep/absent → 4503, the panel's 'asleep' code.

    ``resolve_running_container`` returning ``(row, None)`` covers every
    not-reachable flavor (scaled to zero, mid-replacement, still
    provisioning); the relay must not try to distinguish them.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _fake_decode(token: str):
        return {"sub": "user_test1", "email": "t@example.com"}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            assert owner_id == "user_test1"
            return {"status": "running"}, None

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", cookies={"__session": "ok.jwt"}):
            pass  # pragma: no cover

    assert exc_info.value.code == 4503


def test_ws_viewer_disconnect_tears_down_upstream(monkeypatch):
    """Viewer closes the panel → the upstream socket must close promptly.

    The failure mode this pins: with a gather-both relay, the viewer leg
    returns on disconnect but the container leg stays blocked in ``async for``
    until the container side speaks — which it never does unprompted — so the
    upstream socket (and with it the per-viewer x11vnc polling the owner's
    framebuffer) leaks until process restart. First-completed teardown is the
    fix; this test fails with the gather-both shape.
    """
    import asyncio
    import threading

    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    upstream_closed = threading.Event()

    class _FakeUpstreamConn:
        subprotocol = None

        async def send(self, data):  # viewer bytes are irrelevant here
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            # The container side never speaks unprompted — block until
            # cancelled, exactly like a silent websockify connection.
            await asyncio.Event().wait()

    class _FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return _FakeUpstreamConn()

        async def __aexit__(self, *exc_info):
            upstream_closed.set()
            return False

    async def _fake_decode(token: str):
        return {"sub": "user_test1", "email": "t@example.com"}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            return {"status": "running"}, "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "ws_connect", lambda *a, **k: _FakeConnect())

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", cookies={"__session": "ok.jwt"}) as ws:
        # Close ONLY the viewer socket, inside the context, so the ASGI app
        # keeps running. Exiting the context instead would cancel the whole
        # route task — the harness would then do the cleanup the route failed
        # to do, and this test passed against the buggy gather-both shape
        # exactly that way before this arrangement.
        ws.close()
        assert upstream_closed.wait(timeout=5), "upstream socket leaked after viewer disconnect"


def test_ws_subprotocol_jwt_authenticates_and_binary_is_selected(monkeypatch):
    """The cross-host path: credential in the subprotocol offer, no cookie.

    The panel offers ["binary", <jwt>]; the relay must validate the JWT entry,
    echo "binary" back (a browser that offered subprotocols fails the
    connection if the server selects none), and forward ONLY "binary"
    upstream — the credential must never leave the process.
    """
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _fake_decode(token: str):
        if token == "the.clerk.jwt":
            return {"sub": "user_test1", "email": "t@example.com"}
        raise ValueError("not a credential")

    upstream_subprotocols: dict[str, object] = {}

    class _FakeUpstreamConn:
        subprotocol = "binary"

        async def send(self, data):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeConnect:
        def __init__(self, *args, **kwargs):
            upstream_subprotocols["value"] = kwargs.get("subprotocols")

        async def __aenter__(self):
            return _FakeUpstreamConn()

        async def __aexit__(self, *exc_info):
            return False

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            return {"status": "running"}, "10.0.0.42"

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())
    monkeypatch.setattr(mod, "ws_connect", lambda *a, **k: _FakeConnect(*a, **k))

    client = TestClient(_build_app())
    with client.websocket_connect("/ws", subprotocols=["binary", "the.clerk.jwt"]) as ws:
        assert ws.accepted_subprotocol == "binary"

    assert upstream_subprotocols["value"] == ["binary"]


def test_ws_org_member_resolves_org_container(monkeypatch):
    """Org claims must route to the ORG's container, not a per-user lookup.

    A bare ``user_id`` key misses every org-owned container — the same
    mistake an earlier paperclip relay draft made in reverse. Asserting the
    owner id passed to the resolver pins the resolve_owner_id wiring.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import routers.browser_view as mod

    async def _fake_decode(token: str):
        return {"sub": "user_member", "o": {"id": "org_acme", "rol": "admin", "slg": "acme"}}

    seen: dict[str, str] = {}

    class _FakeEcs:
        async def resolve_running_container(self, owner_id):
            seen["owner_id"] = owner_id
            return None, None

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "get_ecs_manager", lambda: _FakeEcs())

    client = TestClient(_build_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", cookies={"__session": "ok.jwt"}):
            pass  # pragma: no cover

    assert seen["owner_id"] == "org_acme"
