"""The AgentCore bridge's workspace-scoped credential vending door."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.repositories import container_repo, integrations_repo
from core.services import messaging
from core.services.credentials import registry
from routers import runtime_rpc


@pytest.fixture
def credential_app(monkeypatch):
    app = FastAPI()
    app.include_router(runtime_rpc.router, prefix="/api/v1/runtime")
    monkeypatch.setattr(
        container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "hook_token": "owner-hook"}),
    )
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "agent_1"}]))
    sentry = registry.get_provider("sentry").model_copy(update={"refresh_owner": "backend"})
    monkeypatch.setattr(registry, "all_providers", lambda: {"sentry": sentry})
    monkeypatch.setattr(
        integrations_repo, "list_for_owner", AsyncMock(return_value=[{"provider": "sentry", "status": "connected"}])
    )
    return app


@pytest.mark.asyncio
async def test_wrong_workspace_hook_cannot_vend(credential_app):
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer wrong"},
            json={"agent_id": "agent_1"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_agent_outside_roster_cannot_vend(credential_app):
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer owner-hook"},
            json={"agent_id": "other_agent"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_connected_provider_returns_access_token_only(credential_app, monkeypatch):
    from core.services.credentials import access_token

    monkeypatch.setattr(access_token, "get", AsyncMock(return_value="short-access"))
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer owner-hook"},
            json={"agent_id": "agent_1"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"tokens": {"sentry": "short-access"}, "unavailable": []}
    assert "refresh" not in response.text


@pytest.mark.asyncio
async def test_disconnected_provider_returns_no_token(credential_app, monkeypatch):
    from core.services.credentials import access_token

    get = AsyncMock(side_effect=AssertionError("disconnected grant must not be read"))
    monkeypatch.setattr(access_token, "get", get)
    monkeypatch.setattr(integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer owner-hook"},
            json={"agent_id": "agent_1"},
        )
    assert response.status_code == 200
    assert response.json() == {"tokens": {}, "unavailable": []}


@pytest.mark.asyncio
async def test_refresh_failure_returns_unavailable_without_token(credential_app, monkeypatch):
    from core.services.credentials import access_token

    monkeypatch.setattr(access_token, "get", AsyncMock(side_effect=access_token.ReconnectRequired("sentry")))
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer owner-hook"},
            json={"agent_id": "agent_1"},
        )
    assert response.status_code == 200
    assert response.json() == {"tokens": {}, "unavailable": ["sentry"]}


@pytest.mark.asyncio
async def test_dev_only_sentry_is_not_vended_in_production(credential_app, monkeypatch):
    from core.services.credentials import access_token

    sentry = registry.get_provider("sentry").model_copy(update={"refresh_owner": "backend"})
    monkeypatch.setattr(registry, "all_providers", lambda: {"sentry": sentry})
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    get = AsyncMock(side_effect=AssertionError("prod must not vend dev-only Sentry"))
    monkeypatch.setattr(access_token, "get", get)
    async with AsyncClient(transport=ASGITransport(app=credential_app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runtime/credentials/u/org_1",
            headers={"Authorization": "Bearer owner-hook"},
            json={"agent_id": "agent_1"},
        )
    assert response.status_code == 200
    assert response.json() == {"tokens": {}, "unavailable": []}
