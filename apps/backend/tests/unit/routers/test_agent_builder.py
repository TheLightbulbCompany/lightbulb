"""Tests for the Atlas agent-builder bridge (register a built agent).

Covers the hook-token auth gate, the happy path (snapshot → mint Paperclip →
deploy), and the orphan-rollback when the post-mint deploy fails.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.services import agent_export  # noqa: E402
from routers import agent_builder  # noqa: E402

OWNER = "org_abc"
TOKEN = "hook-secret-123"
URL = f"/u/{OWNER}/register-built-agent"
BODY = {"staging_agent_id": "staging-uuid-1", "name": "Lex the Legal Bot"}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agent_builder.router)
    return TestClient(app)


def _pkg():
    from core.services.catalog_package import CatalogPackage

    return CatalogPackage(
        format="openclaw",
        manifest={
            "name": "Lex the Legal Bot",
            "agent_id": "staging-uuid-1",
            "description": "Summarizes contracts",
        },
        openclaw_slice={"agent": {"name": "Lex"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tarball_bytes=b"fake-tar",
        tarball_contents=["./openclaw.json"],
    )


def test_missing_token_401(client):
    resp = client.post(URL, json=BODY)
    assert resp.status_code == 401


@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_wrong_token_401(mock_get_container, client):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw"}
    resp = client.post(URL, json=BODY, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_credential_leak_in_built_slice_400(mock_get_container, mock_export, client):
    """A credential surviving the scrub in the built agent's slice → 400 (the
    export raises CredentialLeakError, which the router maps before any mint)."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.side_effect = agent_export.CredentialLeakError("secret survived: mcp.servers.x.env.API_KEY")
    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 400
    assert "secret survived" in resp.json()["detail"]


@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_happy_path(mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, client):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex the Legal Bot"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "new-agent-uuid"
    assert resp.json()["registered"] is True

    # Snapshot keyed by owner_id + the staging dir.
    assert mock_export.call_args.kwargs["owner_id"] == OWNER
    assert mock_export.call_args.kwargs["staging_agent_id"] == "staging-uuid-1"
    # Deploy threaded the minted UUID as new_agent_id.
    assert catalog.deploy_from_artifact.await_args.kwargs["new_agent_id"] == "new-agent-uuid"
    # The built agent's description (from the manifest) reaches the Paperclip
    # mint via meta — capabilities aren't empty.
    minted_meta = mock_create_pc.await_args.kwargs["meta"]
    assert "Summarizes contracts" in (minted_meta.capabilities or "")


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_container_down_503_before_mint(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_delete_pc, client
):
    """A not-running container 503s BEFORE the Paperclip mint — so no agent is
    created and there's nothing to roll back (avoids mint+delete on every retry
    while the container starts)."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mock_get_ecs.return_value = ecs

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    mock_create_pc.assert_not_awaited()  # nothing minted
    mock_delete_pc.assert_not_awaited()  # nothing to roll back


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_running_container_without_gateway_token_503_before_mint(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_delete_pc, client
):
    """Container row exists + IP resolves, but gateway_token is absent (pre-token
    row / partial write) → 503 before mint, no rollback. The token is read from
    the auth row, so the absence is here, not in resolve_running_container."""
    mock_get_container.return_value = {"hook_token": TOKEN}  # no gateway_token
    mock_export.return_value = _pkg()
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"service_name": "svc"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    mock_create_pc.assert_not_awaited()
    mock_delete_pc.assert_not_awaited()


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_mint_failure_503_no_deploy_no_rollback(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_delete_pc, client
):
    """A Paperclip mint failure (or malformed response) 503s cleanly — the read
    of new_agent_id stays inside the mint try, so it never escapes uncaught into
    a 500, and deploy/rollback are never reached (nothing was created)."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    # Malformed mint response (no "id") → KeyError caught → 503.
    mock_create_pc.return_value = {"name": "Lex"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock()
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    catalog.deploy_from_artifact.assert_not_awaited()  # never reached deploy
    mock_delete_pc.assert_not_awaited()  # nothing minted to roll back


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_failure_rolls_back_paperclip(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_delete_pc, client
):
    """Container is up and the agent is minted, but the deploy into the
    container fails → roll back the orphan Paperclip agent."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("untar failed"))
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "new-agent-uuid"
