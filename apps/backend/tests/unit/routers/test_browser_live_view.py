"""GET /api/v1/browser/live-view — the owner's watch-only AgentCore Browser panel."""

from unittest.mock import patch

import pytest

INACTIVE = {
    "active": False,
    "url": None,
    "expiresAt": None,
    "remoteWidth": 1920,
    "remoteHeight": 1080,
}


@pytest.mark.asyncio
async def test_live_view_is_owner_scoped(async_client_org_admin):
    """The owner id comes from the Clerk claim via resolve_owner_id and from nowhere else:
    a query/body-supplied owner would let any signed-in user watch another workspace's
    browser."""
    with patch("routers.container.browser_sessions.live_view", return_value=INACTIVE) as live_view:
        resp = await async_client_org_admin.get("/api/v1/browser/live-view?owner_id=org_someone_else")

    assert resp.status_code == 200
    live_view.assert_awaited_once_with("org_test_456")


@pytest.mark.asyncio
async def test_live_view_sets_cache_control_no_store(async_client_org_admin):
    """The presigned DCV URL is a 300 s bearer credential for a live browser: it must not
    sit in a CDN, a proxy or the browser's disk cache."""
    active = {**INACTIVE, "active": True, "url": "https://signed", "expiresAt": "2026-09-19T00:05:00+00:00"}
    with patch("routers.container.browser_sessions.live_view", return_value=active):
        resp = await async_client_org_admin.get("/api/v1/browser/live-view")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == active


@pytest.mark.asyncio
async def test_live_view_refuses_a_caller_with_no_workspace(app):
    """resolve_owner_id 409s a caller with no org claim; the panel is no exception to the
    workspace-only fence."""
    from httpx import ASGITransport, AsyncClient

    from core.auth import AuthContext, get_current_user

    async def _personal():
        return AuthContext(user_id="user_alone", email="a@example.com")

    app.dependency_overrides[get_current_user] = _personal
    try:
        with patch("routers.container.browser_sessions.live_view", return_value=INACTIVE) as live_view:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/v1/browser/live-view")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409
    live_view.assert_not_awaited()
