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
from core.services.agent_budget import AGENT_BUDGET_EXCEEDED_DETAIL  # noqa: E402
from core.services.paperclip_admin_client import PaperclipAgentCreateReplayMiss  # noqa: E402
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


def _pkg(description: str = "Summarizes contracts"):
    from core.services.catalog_package import CatalogPackage

    return CatalogPackage(
        format="openclaw",
        manifest={
            "name": "Lex the Legal Bot",
            "agent_id": "staging-uuid-1",
            "description": description,
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


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_happy_path(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_resolve_ceo, client
):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex the Legal Bot"}
    mock_resolve_ceo.return_value = "friday-uuid"
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
    # Healthy build: no degradations to relay.
    assert resp.json()["warnings"] == []

    # Snapshot keyed by owner_id + the staging dir.
    assert mock_export.call_args.kwargs["owner_id"] == OWNER
    assert mock_export.call_args.kwargs["staging_agent_id"] == "staging-uuid-1"
    # Deploy threaded the minted UUID as new_agent_id.
    assert catalog.deploy_from_artifact.await_args.kwargs["new_agent_id"] == "new-agent-uuid"
    # The built agent's description (from the manifest) reaches the Paperclip
    # mint via meta — capabilities aren't empty.
    minted_meta = mock_create_pc.await_args.kwargs["meta"]
    assert "Summarizes contracts" in (minted_meta.capabilities or "")
    # No reports_to in the body → defaulted to the apex CEO (Friday).
    assert mock_create_pc.await_args.kwargs["reports_to"] == "friday-uuid"
    assert mock_create_pc.await_args.kwargs["idempotency_key"].startswith("isol8-agent-create:v1:agent-builder:")


@patch("routers.agent_builder.emit_agent_deployed")
@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
    return_value=("claim-token", None),
)
@patch("routers.agent_builder.is_at_agent_budget", new_callable=AsyncMock, return_value=False)
@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_retry_of_same_staging_build_reuses_key_after_display_metadata_changes(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_get_catalog,
    mock_resolve_ceo,
    _at_budget,
    _claim,
    _release,
    mock_emit,
    client,
):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.side_effect = [
        {"id": "same-agent-uuid", "name": "Lex"},
        {"id": "same-agent-uuid", "name": "Renamed Lex"},
    ]
    mock_resolve_ceo.return_value = "friday-uuid"
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.live_install_for_agent.side_effect = [None, {"agent_id": "same-agent-uuid"}]
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "same-agent-uuid"})
    mock_get_catalog.return_value = catalog

    first = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    second = client.post(
        URL,
        json={**BODY, "name": "Renamed Lex", "reports_to": "different-manager"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_key = mock_create_pc.await_args_list[0].kwargs["idempotency_key"]
    second_key = mock_create_pc.await_args_list[1].kwargs["idempotency_key"]
    assert first_key == second_key
    assert mock_create_pc.await_args_list[0].kwargs["idempotency_replay_only"] is False
    assert mock_create_pc.await_args_list[1].kwargs["idempotency_replay_only"] is False
    catalog.deploy_from_artifact.assert_awaited_once()
    assert catalog.live_install_for_agent.call_count == 2
    mock_emit.assert_called_once()


@patch("routers.agent_builder.emit_agent_deployed")
@patch("routers.agent_builder.ensure_operator_device_paired", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday")
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_committed_install_reconciliation_failure_preserves_install(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_get_catalog,
    mock_delete_pc,
    _mock_ceo,
    mock_pair,
    mock_emit,
    client,
):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "same-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.live_install_for_agent.return_value = {"agent_id": "same-agent-uuid"}
    catalog.deploy_from_artifact = AsyncMock()
    catalog.unwind_partial_deploy = AsyncMock()
    mock_get_catalog.return_value = catalog
    mock_pair.side_effect = RuntimeError("device registration unavailable")

    response = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 502
    assert "reconcile existing container install failed" in response.json()["detail"]
    catalog.deploy_from_artifact.assert_not_awaited()
    catalog.unwind_partial_deploy.assert_not_awaited()
    mock_delete_pc.assert_not_awaited()
    mock_emit.assert_not_called()


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
    return_value=("claim-token", None),
)
@patch("routers.agent_builder.is_at_agent_budget", new_callable=AsyncMock, return_value=True)
@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_exact_cap_builder_retry_is_replay_only_and_cannot_create(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_resolve_ceo,
    _at_budget,
    _claim,
    release,
    client,
):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_resolve_ceo.return_value = "friday-uuid"
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    mock_create_pc.side_effect = PaperclipAgentCreateReplayMiss(
        "agent create replay missed",
        409,
        {"code": "agent_create_idempotency_replay_miss"},
    )

    response = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 409
    assert response.json()["detail"] == AGENT_BUDGET_EXCEEDED_DETAIL
    assert mock_create_pc.await_args.kwargs["idempotency_replay_only"] is True
    assert mock_create_pc.await_args.kwargs["idempotency_key"].startswith("isol8-agent-create:v1:agent-builder:")
    release.assert_awaited_once()


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_explicit_reports_to_skips_ceo_default(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_resolve_ceo, client
):
    """A reports_to in the body wins — no CEO lookup, forwarded verbatim."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog

    body = {**BODY, "reports_to": "manager-uuid"}
    resp = client.post(URL, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    mock_resolve_ceo.assert_not_awaited()
    assert mock_create_pc.await_args.kwargs["reports_to"] == "manager-uuid"


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_degradations_surface_as_warnings(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_resolve_ceo, client
):
    """Blank description, unresolved CEO, and sanitizer-dropped keys each land
    in ``warnings`` (registration still succeeds) — instead of dying in logs,
    which is how the founders' first built agent shipped with a blank
    org-chart card."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg(description="")
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    mock_resolve_ceo.return_value = None
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={"agent_id": "new-agent-uuid", "agent_entry_keys_dropped": ["archetype"]}
    )
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["registered"] is True
    warnings = resp.json()["warnings"]
    assert any("no description" in w for w in warnings)
    assert any("could not resolve the company CEO" in w for w in warnings)
    assert any("archetype" in w for w in warnings)
    # Unresolved CEO → mint proceeds unparented, matching the old behavior.
    assert mock_create_pc.await_args.kwargs["reports_to"] is None


@patch("routers.agent_builder.ensure_operator_device_paired", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_happy_path_pairs_operator_device(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, _mock_ceo, mock_pair, client
):
    """The full Paperclip mint response — carrying adapterConfig.devicePrivateKeyPem
    — must reach ensure_operator_device_paired verbatim. A stub dict or bare id
    would early-return inside the pairing helper and leave the agent dead
    (unauthorized: gateway token mismatch on its first run)."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    full_paperclip_agent = {
        "id": "new-agent-uuid",
        "name": "Lex the Legal Bot",
        "adapterType": "openclaw_gateway",
        "adapterConfig": {"devicePrivateKeyPem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"},
    }
    mock_create_pc.return_value = full_paperclip_agent
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text

    mock_pair.assert_awaited_once()
    kwargs = mock_pair.await_args.kwargs
    assert kwargs["owner_id"] == OWNER
    # The full mint dict (identity, not equality) — not the slice, not just an id.
    assert kwargs["paperclip_agent"] is full_paperclip_agent
    assert kwargs["paperclip_agent"]["adapterConfig"]["devicePrivateKeyPem"]
    assert kwargs["gateway_token"] == "gw-token"
    assert kwargs["ip"] == "10.0.0.1"


@patch("routers.agent_builder.emit_agent_deployed")
@patch("routers.agent_builder.ensure_operator_device_paired", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_a_build_rolled_back_by_a_pairing_failure_is_not_counted_as_deployed(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_get_catalog,
    _mock_ceo,
    mock_pair,
    mock_emit,
    client,
):
    """Pairing sits inside this route's rollback envelope — a failure there
    unwinds the install and deletes the Paperclip agent. That is not a
    deployment, so the funnel event must not have fired already."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    catalog.unwind_partial_deploy = AsyncMock(return_value=True)
    mock_get_catalog.return_value = catalog
    mock_pair.side_effect = RuntimeError("gateway refused the device")

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502, resp.text
    mock_emit.assert_not_called()


@patch("routers.agent_builder.ensure_operator_device_paired", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_pairing_happens_after_deploy_not_before(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, _mock_ceo, mock_pair, client
):
    call_order: list[str] = []
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {
        "id": "new-agent-uuid",
        "adapterType": "openclaw_gateway",
        "adapterConfig": {"devicePrivateKeyPem": "pem"},
    }
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()

    async def _deploy(**_kwargs):
        call_order.append("deploy")
        return {"agent_id": "new-agent-uuid"}

    async def _pair(**_kwargs):
        call_order.append("pair")

    catalog.deploy_from_artifact = AsyncMock(side_effect=_deploy)
    mock_pair.side_effect = _pair
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert call_order == ["deploy", "pair"]


@patch("routers.agent_builder.ensure_operator_device_paired", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_failure_skips_operator_pairing(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_get_catalog,
    mock_delete_pc,
    _mock_ceo,
    mock_pair,
    client,
):
    """A deploy failure never reaches the pairing call — it's gated on a
    successful deploy, inside the same try the rollback below unwinds."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("untar failed"))
    catalog.unwind_partial_deploy = AsyncMock()
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502
    mock_pair.assert_not_awaited()


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


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_mint_failure_503_no_deploy_no_rollback(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_delete_pc, _mock_ceo, client
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


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_failure_rolls_back_paperclip(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_delete_pc, _mock_ceo, client
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
    catalog.unwind_partial_deploy = AsyncMock()
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502
    # The detail must carry the failure class + message: Atlas relays exact
    # errors to the task thread, and a bare "deploy failed" leaves everyone
    # (agent and operator) retrying blind against a deterministic failure.
    assert "RuntimeError: untar failed" in resp.json()["detail"]
    # Container/EFS side effects are unwound BEFORE the Paperclip rollback,
    # mirroring the marketplace envelope — otherwise every failed attempt
    # strands a half-installed name=<uuid> ghost in the owner's config (and a
    # validation-failing entry keeps the whole config invalid).
    catalog.unwind_partial_deploy.assert_awaited_once()
    unwind_kwargs = catalog.unwind_partial_deploy.await_args.kwargs
    assert unwind_kwargs["owner_id"] == OWNER
    assert unwind_kwargs["new_agent_id"] == "new-agent-uuid"
    assert isinstance(unwind_kwargs["cause"], RuntimeError)
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "new-agent-uuid"


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_timeout_passes_cause_and_keeps_both_sides_of_the_install(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, mock_delete_pc, _mock_ceo, client
):
    """deploy_from_artifact's pair RPC can raise TimeoutError (no answer
    yet, not necessarily a failure). The router still calls
    unwind_partial_deploy unconditionally — the skip decision lives in
    that shared function (see test_catalog_service.py) — and passes the
    TimeoutError as ``cause``. When that function reports it SKIPPED the
    unwind, the container-side install is still standing, so the Paperclip
    row is kept with it: deleting it alone leaves an agent in the container
    that nobody can see or remove from Teams."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    # The real one returns False when it SKIPS the unwind on a timeout.
    catalog.unwind_partial_deploy = AsyncMock(return_value=False)
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502

    catalog.unwind_partial_deploy.assert_awaited_once()
    unwind_kwargs = catalog.unwind_partial_deploy.await_args.kwargs
    assert unwind_kwargs["new_agent_id"] == "new-agent-uuid"
    assert isinstance(unwind_kwargs["cause"], TimeoutError)
    mock_delete_pc.assert_not_awaited()


# ---------------------------------------------------------------------------
# Owner-wide deploy claim — this path calls deploy_from_artifact (and through
# it pair_with_openclaw) against the one running container, so an Atlas
# registration racing a marketplace Hire is the 2026-08-06 incident with a
# different second caller.
# ---------------------------------------------------------------------------


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_takes_and_releases_the_owner_deploy_claim(
    mock_get_container, mock_export, mock_create_pc, mock_get_ecs, mock_get_catalog, _ceo, claim, release, client
):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog
    claim.return_value = ("tok_b", None)

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200, resp.text
    claim.assert_awaited_once()
    # Released with the fencing token — a leaked claim wedges every install this
    # owner attempts for the whole lease.
    release.assert_awaited_once()
    assert release.await_args.kwargs["token"] == "tok_b"


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_second_concurrent_register_is_refused_409_before_minting(
    mock_get_container, mock_export, mock_create_pc, claim, release, client
):
    """Fast 409, never a server-side wait — and the claim is taken BEFORE the
    Paperclip mint, so a refusal has nothing to undo."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    claim.return_value = (None, "lst_other")

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "deploy_busy"
    mock_export.assert_not_called()
    mock_create_pc.assert_not_awaited()
    release.assert_not_awaited()


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_refusal_by_this_same_build_says_do_not_retry(mock_get_container, claim, _release, client):
    """Retrying THIS build registers a second copy — there is no idempotency
    check on this path — so the same-build refusal carries no retry advice."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    claim.return_value = (None, f"builder:{BODY['staging_agent_id']}")

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "deploy_in_progress"}


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_claim_is_released_when_the_registration_fails(mock_get_container, mock_export, claim, release, client):
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.side_effect = agent_export.AgentNotFoundError("no such staging dir")
    claim.return_value = ("tok_b", None)

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 404
    release.assert_awaited_once()
    assert release.await_args.kwargs["token"] == "tok_b"


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.get_ecs_manager")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_a_timed_out_registration_holds_the_claim_through_the_502_wrap(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_ecs,
    mock_get_catalog,
    _delete_pc,
    _ceo,
    claim,
    release,
    client,
):
    """Same defect as the marketplace route, one wrap deeper. A timed-out
    registration leaves the container-side install standing (the unwind is
    skipped), so freeing the claim would let the next install run on top of it.
    This path reports the failure as a 502 raised ``from`` the timeout, so the
    release has to see through the wrap to hold the claim.
    """
    from core.repositories.container_repo import _has_timeout_cause

    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-token"}, "10.0.0.1"))
    mock_get_ecs.return_value = ecs
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    catalog.unwind_partial_deploy = AsyncMock(return_value=False)
    mock_get_catalog.return_value = catalog
    claim.return_value = ("tok_b", None)

    assert client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 502

    release.assert_awaited_once()
    assert _has_timeout_cause(release.await_args.kwargs["cause"]), "the 502-wrapped timeout must still hold the claim"
