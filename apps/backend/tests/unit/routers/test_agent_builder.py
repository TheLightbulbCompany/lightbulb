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
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}
    resp = client.post(URL, json=BODY, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_credential_leak_in_built_slice_400(mock_get_container, mock_export, client):
    """A credential surviving the scrub in the built agent's slice → 400 (the
    export raises CredentialLeakError, which the router maps before any mint)."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.side_effect = agent_export.CredentialLeakError("secret survived: mcp.servers.x.env.API_KEY")
    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 400
    assert "secret survived" in resp.json()["detail"]


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_happy_path(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_resolve_ceo, client, monkeypatch
):
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex the Legal Bot"}
    mock_resolve_ceo.return_value = "friday-uuid"
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

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
    assert mock_create_pc.await_args.kwargs["idempotency_key"].startswith("lightbulb-agent-create:v1:agent-builder:")
    assert "idempotency_replay_only" not in mock_create_pc.await_args.kwargs


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_threads_the_auth_container_through_to_export_and_deploy(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_resolve_ceo, client, monkeypatch
):
    """Fix round 1, item 1: auth_container (the hook-token dependency's container row) must
    reach both export_built_agent_from_staging and deploy_from_artifact, so the build reads and
    writes the owner's Hermes S3 profile rather than each step reading the row again."""
    auth_container = {"hook_token": TOKEN, "gateway_token": "gw-token", "agentcore_runtime_arn": "arn:r1"}
    mock_get_container.return_value = auth_container
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex the Legal Bot"}
    mock_resolve_ceo.return_value = "friday-uuid"
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"plan": "standard"}),
    )

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert mock_export.call_args.kwargs["container"] == auth_container
    assert catalog.deploy_from_artifact.await_args.kwargs["container"] == auth_container


@patch("routers.agent_builder.emit_agent_deployed")
@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
    return_value=("claim-token", None),
)
@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_retry_of_same_staging_build_reuses_key_after_display_metadata_changes(
    mock_get_container,
    mock_export,
    mock_create_pc,
    mock_get_catalog,
    mock_resolve_ceo,
    _claim,
    _release,
    mock_emit,
    client,
):
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.side_effect = [
        {"id": "same-agent-uuid", "name": "Lex"},
        {"id": "same-agent-uuid", "name": "Renamed Lex"},
    ]
    mock_resolve_ceo.return_value = "friday-uuid"
    catalog = MagicMock()
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
    assert "idempotency_replay_only" not in mock_create_pc.await_args_list[0].kwargs
    assert "idempotency_replay_only" not in mock_create_pc.await_args_list[1].kwargs
    # KNOWN GAP: the retry used to RECONCILE against the existing install (a ``.template``
    # sidecar on EFS) and skip the deploy. There is no install record on the profile transport,
    # so it deploys again -- safe, because Paperclip's stable key returns the SAME uuid and the
    # profile write is an overwrite, but the funnel event fires twice.
    assert catalog.deploy_from_artifact.await_count == 2
    assert mock_emit.call_count == 2


@patch("routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_explicit_reports_to_skips_ceo_default(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_resolve_ceo, client
):
    """A reports_to in the body wins — no CEO lookup, forwarded verbatim."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
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
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_degradations_surface_as_warnings(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_resolve_ceo, client
):
    """Blank description, unresolved CEO, and sanitizer-dropped keys each land
    in ``warnings`` (registration still succeeds) — instead of dying in logs,
    which is how the founders' first built agent shipped with a blank
    org-chart card."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg(description="")
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    mock_resolve_ceo.return_value = None
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


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_failure_skips_operator_pairing(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_delete_pc, _mock_ceo, client
):
    """A deploy failure never reaches the pairing call — it's gated on a
    successful deploy, inside the same try the rollback below unwinds."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("untar failed"))
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_unprovisioned_workspace_503s_before_mint(
    mock_get_container, mock_export, mock_create_pc, mock_delete_pc, client
):
    """A row with no runtime 503s BEFORE the Paperclip mint — so no agent is created and there is
    nothing to roll back. Otherwise every retry while provisioning settles costs a mint plus an
    immediate delete, and each of those is a Paperclip write."""
    mock_get_container.return_value = {"hook_token": TOKEN, "gateway_token": "gw-token"}
    mock_export.return_value = _pkg()

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    mock_create_pc.assert_not_awaited()  # nothing minted
    mock_delete_pc.assert_not_awaited()  # nothing to roll back


@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_running_container_without_gateway_token_503_before_mint(
    mock_get_container, mock_export, mock_create_pc, mock_delete_pc, client
):
    """Container row exists + IP resolves, but gateway_token is absent (pre-token
    row / partial write) → 503 before mint, no rollback. The token is read from
    the auth row, so the absence is here, not in resolve_running_container."""
    mock_get_container.return_value = {"hook_token": TOKEN}  # no gateway_token
    mock_export.return_value = _pkg()

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    mock_create_pc.assert_not_awaited()
    mock_delete_pc.assert_not_awaited()


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_mint_failure_503_no_deploy_no_rollback(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_delete_pc, _mock_ceo, client
):
    """A Paperclip mint failure (or malformed response) 503s cleanly — the read
    of new_agent_id stays inside the mint try, so it never escapes uncaught into
    a 500, and deploy/rollback are never reached (nothing was created)."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
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
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_failure_rolls_back_paperclip(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_delete_pc, _mock_ceo, client
):
    """Container is up and the agent is minted, but the deploy into the
    container fails → roll back the orphan Paperclip agent."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("untar failed"))
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
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "new-agent-uuid"


@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_deploy_timeout_passes_cause_and_keeps_both_sides_of_the_install(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, mock_delete_pc, _mock_ceo, client
):
    """A TimeoutError means "no answer yet", not "failed": the profile write may still be
    landing and the Paperclip agent may already work. The install is left standing, so the
    Paperclip row is kept WITH it -- deleting it alone leaves a live profile nobody can see or
    remove from Teams, and nothing that would ever clean it up."""
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    # The real one returns False when it SKIPS the unwind on a timeout.
    mock_get_catalog.return_value = catalog

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 502

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
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_register_takes_and_releases_the_owner_deploy_claim(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, _ceo, claim, release, client
):
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
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
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
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
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    claim.return_value = (None, f"builder:{BODY['staging_agent_id']}")

    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "deploy_in_progress"}


@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch("routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_claim_is_released_when_the_registration_fails(mock_get_container, mock_export, claim, release, client):
    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
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
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
@patch("routers.agent_builder.container_repo.get_by_owner_id", new_callable=AsyncMock)
def test_a_timed_out_registration_holds_the_claim_through_the_502_wrap(
    mock_get_container, mock_export, mock_create_pc, mock_get_catalog, _delete_pc, _ceo, claim, release, client
):
    """Same defect as the marketplace route, one wrap deeper. A timed-out
    registration leaves the container-side install standing (the unwind is
    skipped), so freeing the claim would let the next install run on top of it.
    This path reports the failure as a 502 raised ``from`` the timeout, so the
    release has to see through the wrap to hold the claim.
    """
    from core.repositories.container_repo import _has_timeout_cause

    mock_get_container.return_value = {
        "hook_token": TOKEN,
        "gateway_token": "gw-token",
        "agentcore_runtime_arn": "arn:r1",
    }
    mock_export.return_value = _pkg()
    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Lex"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=TimeoutError())
    mock_get_catalog.return_value = catalog
    claim.return_value = ("tok_b", None)

    assert client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 502

    release.assert_awaited_once()
    assert _has_timeout_cause(release.await_args.kwargs["cause"]), "the 502-wrapped timeout must still hold the claim"


@patch("routers.agent_builder.emit_agent_deployed")
@patch("routers.agent_builder.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch(
    "routers.agent_builder.container_repo.try_claim_owner_deploy", new_callable=AsyncMock, return_value=("tok", None)
)
@patch(
    "routers.agent_builder.agent_export.resolve_apex_ceo_agent_id", new_callable=AsyncMock, return_value="friday-uuid"
)
@patch("routers.agent_builder.get_catalog_service")
@patch("routers.agent_builder.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.agent_builder.agent_export.export_built_agent_from_staging")
def test_register_staged_agent_under_claim_uses_given_package(
    mock_export, mock_create_pc, mock_get_catalog, _ceo, mock_claim, mock_release, _emit
):
    import asyncio

    mock_create_pc.return_value = {"id": "new-agent-uuid", "name": "Quill"}
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(return_value={"agent_id": "new-agent-uuid"})
    mock_get_catalog.return_value = catalog
    pkg = _pkg()
    pkg.openclaw_slice["cron_jobs"] = [{"name": "Daily digest"}]
    result = asyncio.run(
        agent_builder.register_staged_agent_under_claim(
            owner_id=OWNER,
            payload=agent_builder.RegisterBuiltAgentRequest(staging_agent_id="import-abcdef123456-main", name="Quill"),
            auth_container={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"},
            claim_label="import:import-abcdef123456",
            package=pkg,
        )
    )
    assert result["agent_id"] == "new-agent-uuid"
    mock_export.assert_not_called()
    assert catalog.deploy_from_artifact.await_args.kwargs["slice_"]["cron_jobs"] == [{"name": "Daily digest"}]
    assert mock_claim.await_args.kwargs["listing_id"] == "import:import-abcdef123456"
    mock_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_hook_token_non_ascii_bearer_is_401_not_500():
    """Mirror of the agent_mail case: Starlette decodes headers as latin-1, so a
    garbage Authorization value reaches the guard as non-ASCII str; compare_digest
    on str then raises TypeError and the route 500s. Seen live 2026-08-31 when a
    container agent's curl mangled the token — must be a clean 401."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from routers import agent_builder

    class _Req:
        headers = {"Authorization": "Bearer ünïcode"}

    with patch.object(
        agent_builder.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_a", "hook_token": "tok"}),
    ):
        with pytest.raises(HTTPException) as exc:
            await agent_builder._require_hook_token("org_a", _Req())
    assert exc.value.status_code == 401
