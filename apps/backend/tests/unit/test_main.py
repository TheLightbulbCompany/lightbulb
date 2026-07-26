"""Unit tests for ``main.py``-level wiring.

Currently exercises the ``HostDispatcherMiddleware`` (T16): when a
request arrives with ``X-Isol8-Public-Host`` matching a Paperclip-proxy
host, the middleware rewrites the ASGI scope path to the proxy mount
prefix so the standard router layer dispatches to ``paperclip_proxy``.
For any other host (or absence of the header) the middleware passes
through unchanged.

These tests poke the middleware directly with fabricated ASGI scopes
rather than going through the real FastAPI app — that lets us assert
on the rewritten scope without spinning up the entire request/response
pipeline (which would require Clerk + Paperclip infra to be mocked).
End-to-end integration through the real router is covered by the
docker-compose suite in T18-T20.
"""

from __future__ import annotations

import pytest

from main import (
    PAPERCLIP_PROXY_PREFIX,
    HostDispatcherMiddleware,
    _paperclip_dispatch_hosts,
)


def _make_scope(
    *,
    path: str = "/",
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> dict:
    """Build a minimal ASGI scope for middleware testing.

    Mirrors what uvicorn / starlette would hand the app — only the
    fields the middleware reads need to be present.
    """
    s: dict = {
        "type": scope_type,
        "path": path,
        "headers": headers or [],
        "method": "GET",
        "query_string": b"",
    }
    if raw_path is not None:
        s["raw_path"] = raw_path
    return s


class _CapturingApp:
    """Stand-in ASGI app that records the scope passed to it."""

    def __init__(self) -> None:
        self.last_scope: dict | None = None
        self.call_count = 0

    async def __call__(self, scope, receive, send) -> None:  # noqa: D401
        self.last_scope = scope
        self.call_count += 1


@pytest.mark.asyncio
async def test_dispatcher_rewrites_path_for_company_isol8_host():
    """X-Isol8-Public-Host: company.isol8.co triggers the proxy prefix rewrite."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    # Override hosts to a known set so the test isn't dependent on the
    # CDK-driven settings.PAPERCLIP_PUBLIC_URL value.
    mw._hosts = {"company.isol8.co", "company-dev.isol8.co"}

    scope = _make_scope(
        path="/teams/inbox",
        raw_path=b"/teams/inbox",
        headers=[(b"x-isol8-public-host", b"company.isol8.co")],
    )
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.call_count == 1
    assert inner.last_scope is not None
    assert inner.last_scope["path"] == f"{PAPERCLIP_PROXY_PREFIX}/teams/inbox"
    assert inner.last_scope["raw_path"] == PAPERCLIP_PROXY_PREFIX.encode("ascii") + b"/teams/inbox"


@pytest.mark.asyncio
async def test_dispatcher_rewrites_websocket_scope():
    """WebSocket scope is rewritten the same way as HTTP."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    scope = _make_scope(
        path="/api/socket",
        raw_path=b"/api/socket",
        headers=[(b"x-isol8-public-host", b"company.isol8.co")],
        scope_type="websocket",
    )
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.last_scope is not None
    assert inner.last_scope["path"] == f"{PAPERCLIP_PROXY_PREFIX}/api/socket"


@pytest.mark.asyncio
async def test_dispatcher_passthrough_for_api_isol8_host():
    """Standard API host is NOT rewritten — passes through unchanged."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    scope = _make_scope(
        path="/api/v1/users",
        headers=[(b"x-isol8-public-host", b"api.isol8.co")],
    )
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.last_scope is not None
    assert inner.last_scope["path"] == "/api/v1/users"
    # Object identity matters here: passthrough hands the SAME scope dict
    # to the inner app — no copy. The rewrite branch creates a new dict.
    assert inner.last_scope is scope


@pytest.mark.asyncio
async def test_dispatcher_passthrough_when_xph_header_missing():
    """No X-Isol8-Public-Host header → passthrough (e.g. direct ALB hit)."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    scope = _make_scope(path="/health", headers=[])
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.last_scope is scope


@pytest.mark.asyncio
async def test_dispatcher_strips_port_and_lowercases():
    """X-Isol8-Public-Host is normalized: lowercased + port stripped."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    scope = _make_scope(
        path="/x",
        headers=[(b"x-isol8-public-host", b"Company.Isol8.Co:443")],
    )
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.last_scope is not None
    assert inner.last_scope["path"].startswith(PAPERCLIP_PROXY_PREFIX)


@pytest.mark.asyncio
async def test_dispatcher_handles_comma_chained_xph():
    """Multi-proxy chain → take leftmost element (closest to client)."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    # CDN-style chain: ``company.isol8.co, internal.lb.amazonaws.com``
    scope = _make_scope(
        path="/x",
        headers=[(b"x-isol8-public-host", b"company.isol8.co, internal.lb.amazonaws.com")],
    )
    await mw(scope, lambda: None, lambda *_: None)

    assert inner.last_scope is not None
    assert inner.last_scope["path"].startswith(PAPERCLIP_PROXY_PREFIX)


@pytest.mark.asyncio
async def test_dispatcher_skips_non_http_websocket_scopes():
    """``lifespan`` and other scope types pass straight through."""
    inner = _CapturingApp()
    mw = HostDispatcherMiddleware(inner)
    mw._hosts = {"company.isol8.co"}

    scope = {
        "type": "lifespan",
        "headers": [(b"x-isol8-public-host", b"company.isol8.co")],
    }
    await mw(scope, lambda: None, lambda *_: None)

    # Even a Paperclip-host header doesn't trigger a rewrite for non-
    # request scopes — those don't have a ``path`` to rewrite.
    assert inner.last_scope is scope


def test_dispatch_hosts_includes_localhost_alias():
    """Local-dev hostnames are baked in so ./scripts/local-dev.sh works."""
    hosts = _paperclip_dispatch_hosts()
    assert "company.localhost" in hosts


def test_dispatch_hosts_includes_settings_public_url(monkeypatch):
    """settings.PAPERCLIP_PUBLIC_URL hostname is added to the dispatch set."""
    from core import config as core_config

    monkeypatch.setattr(
        core_config.settings,
        "PAPERCLIP_PUBLIC_URL",
        "https://company-dev.isol8.co",
        raising=False,
    )
    hosts = _paperclip_dispatch_hosts()
    assert "company-dev.isol8.co" in hosts
