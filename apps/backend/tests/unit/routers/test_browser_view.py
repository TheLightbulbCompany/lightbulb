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

    response = TestClient(_build_app()).post(
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


def test_webrtc_proxy_uses_only_the_resolved_private_neko_endpoint(monkeypatch):
    """Client host, forwarded headers, and Isol8's cookie never reach Neko."""
    import httpx
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

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
            return httpx.Response(200, stream=_Stream(), headers={"content-type": "text/plain"}, request=request)

        async def aclose(self):
            seen["closed"] = True

    monkeypatch.setattr(mod, "_decode_token", _fake_decode)
    monkeypatch.setattr(mod, "_resolve_running", _fake_resolve)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    response = TestClient(_build_app()).get(
        "/webrtc/api/whoami",
        headers={"X-Forwarded-For": "198.51.100.8", "X-Real-IP": "198.51.100.8", "Host": "attacker.example"},
        cookies={"isol8_browser_view": "clerk.jwt", "neko": "neko.session", "__session": "never-forward"},
    )

    assert response.status_code == 200
    assert response.content == b"neko"
    assert seen["url"] == "http://10.0.0.42:6081/api/v1/browser-view/webrtc/api/whoami"
    assert seen["headers"] == {"accept": "*/*", "accept-encoding": "gzip, deflate", "cookie": "neko=neko.session"}
    assert seen["closed"] is True


def test_webrtc_unsafe_request_rejects_cross_site_origin_before_resolve(monkeypatch):
    """Cookie authentication alone never authorizes a cross-site mutation."""
    from fastapi.testclient import TestClient

    import routers.browser_view as mod

    async def _unexpected_resolve(*args):
        raise AssertionError("origin rejection must happen before upstream resolution")

    monkeypatch.setattr(mod, "_resolve_running", _unexpected_resolve)
    response = TestClient(_build_app()).post(
        "/webrtc/api/screen",
        headers={"Origin": "https://attacker.example"},
        cookies={"isol8_browser_view": "bad", "neko": "neko.session"},
    )

    assert response.status_code == 403


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
