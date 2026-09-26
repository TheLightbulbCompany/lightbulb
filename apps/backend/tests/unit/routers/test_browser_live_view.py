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
    with patch("routers.container.browser_control.view", return_value=INACTIVE) as live_view:
        resp = await async_client_org_admin.get("/api/v1/browser/live-view?owner_id=org_someone_else")

    assert resp.status_code == 200
    live_view.assert_awaited_once_with("org_test_456", "user_test_123")


@pytest.mark.asyncio
async def test_live_view_sets_cache_control_no_store(async_client_org_admin):
    """The presigned DCV URL is a 300 s bearer credential for a live browser: it must not
    sit in a CDN, a proxy or the browser's disk cache."""
    active = {**INACTIVE, "active": True, "url": "https://signed", "expiresAt": "2026-09-19T00:05:00+00:00"}
    with patch("routers.container.browser_control.view", return_value=active):
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
        with patch("routers.container.browser_control.view", return_value=INACTIVE) as live_view:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/v1/browser/live-view")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409
    live_view.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_start_uses_clerk_workspace_and_identity(async_client_org_admin):
    with (
        patch("routers.container.browser_control.start", return_value=INACTIVE) as start,
        patch("routers.container._assert_provision_allowed"),
    ):
        response = await async_client_org_admin.post("/api/v1/browser/start", json={"owner_id": "org_other"})
    assert response.status_code == 200
    start.assert_awaited_once_with("org_test_456", "user_test_123")


@pytest.mark.asyncio
async def test_internal_browser_refuses_wrong_workspace_hook(app):
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock

    with patch(
        "routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "owner-secret"})
    ):
        with patch("routers.container.browser_control.acquire_agent") as acquire:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/internal/browser/org_other/acquire",
                    headers={"Authorization": "Bearer wrong"},
                    json={"runId": "run"},
                )
    assert response.status_code == 401
    acquire.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_workspace_cannot_start_billed_browser(async_client_org_admin):
    from fastapi import HTTPException

    with patch(
        "routers.container._assert_provision_allowed",
        side_effect=HTTPException(status_code=402, detail="subscription required"),
    ):
        with patch("routers.container.browser_control.start") as start:
            response = await async_client_org_admin.post("/api/v1/browser/start", json={})
    assert response.status_code == 402
    start.assert_not_called()


@pytest.mark.asyncio
async def test_input_uses_clerk_workspace_and_skips_the_provision_gate(async_client_org_admin):
    """A person's input in the live view is a signal, not a grant: owner from the claim only,
    and no billing gate on a request the panel sends every few seconds."""
    with (
        patch("routers.container._assert_provision_allowed") as gate,
        patch("routers.container.browser_control.record_input", return_value={"ok": True}) as record,
    ):
        response = await async_client_org_admin.post("/api/v1/browser/input", json={"owner_id": "org_other"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    record.assert_awaited_once_with("org_test_456")
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_is_any_members_and_uses_clerk_workspace(async_client_org_admin):
    with patch("routers.container.browser_control.stop", return_value=INACTIVE) as stop:
        response = await async_client_org_admin.post("/api/v1/browser/stop", json={"owner_id": "org_other"})
    assert response.status_code == 200
    stop.assert_awaited_once_with("org_test_456", "user_test_123")


@pytest.mark.asyncio
async def test_take_control_and_navigate_are_gone(async_client_org_admin):
    for path in ("/api/v1/browser/control", "/api/v1/browser/navigate"):
        response = await async_client_org_admin.post(path, json={"action": "take", "generation": "g1"})
        assert response.status_code in (404, 405)
