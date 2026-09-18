"""credentials.reproject — re-projecting an already-connected owner into their Hermes profiles.

Every provider used here is a REAL ``providers.yaml`` entry, so the branch a test exercises is
the branch that provider actually takes in prod: ``github`` (CLI materializer, refresh_owner
none → retained secret), ``google`` (CLI materializer, refresh_owner actor → no secret),
``meta_ads`` (MCP-shaped, retained), ``vercel_cli`` (a materializer ``hermes_cli`` does not
support), ``shopify`` (backend_only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.services.credentials import reproject as mod

OWNER = "org_reproject"


def _rows(*providers: str) -> list[dict]:
    return [{"owner_id": OWNER, "provider": p, "status": "connected"} for p in providers]


@pytest.fixture
def wired(monkeypatch):
    """AgentCore owner, no real AWS: the integrations table, Secrets Manager, both projection
    rails and the metric emitter are all stand-ins."""
    monkeypatch.setattr(mod.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True))
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.hermes_cli, "project", AsyncMock())
    reconcile = AsyncMock()
    monkeypatch.setattr("core.services.credentials.mcp_server.reconcile_integrations", reconcile)
    monkeypatch.setattr(mod, "put_metric", lambda *a, **kw: None)
    return reconcile


async def test_retained_cli_provider_is_projected_with_its_secret(wired, monkeypatch):
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("github")))
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value={"token": "ghp_x"}))

    assert await mod.reproject_connections(OWNER) == {"github": "projected"}

    mod.hermes_cli.project.assert_awaited_once_with(OWNER, "github", "gh_cli", {"token": "ghp_x"})


async def test_mcp_providers_share_one_reconcile_call(wired, monkeypatch):
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("meta_ads", "apollo")))
    # meta_ads retains its grant (refresh_owner: none); apollo is actor-refresh and has none.
    monkeypatch.setattr(
        mod.integration_secret_store,
        "get",
        AsyncMock(side_effect=lambda _owner, provider: {"access_token": "t"} if provider == "meta_ads" else None),
    )

    outcomes = await mod.reproject_connections(OWNER)

    assert outcomes == {"meta_ads": "projected", "apollo": "needs_reconnect"}
    wired.assert_awaited_once_with(OWNER, agentcore=True)
    mod.hermes_cli.project.assert_not_awaited()


async def test_actor_refresh_provider_needs_reconnect_and_is_never_projected(wired, monkeypatch):
    """google retains no secret — the container's EFS keyring was the sole custodian, so there
    is nothing to re-project and only the owner can fix it."""
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("google")))

    assert await mod.reproject_connections(OWNER) == {"google": "needs_reconnect"}

    mod.hermes_cli.project.assert_not_awaited()


async def test_unsupported_materializer_and_backend_only_provider(wired, monkeypatch):
    """A materializer with no Hermes projection reports ``unsupported``; a ``backend_only``
    provider's credential never reaches a runtime at all, so it is not reported."""
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("vercel_cli", "shopify")))
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value={"token": "t"}))

    assert await mod.reproject_connections(OWNER) == {"vercel_cli": "unsupported"}


async def test_one_provider_failing_does_not_stop_the_others(wired, monkeypatch):
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("github", "aws")))
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value={"token": "t"}))

    async def _project(_owner, provider, *_args):
        if provider == "github":
            raise RuntimeError("boom")

    monkeypatch.setattr(mod.hermes_cli, "project", AsyncMock(side_effect=_project))

    assert await mod.reproject_connections(OWNER) == {"github": "error:RuntimeError", "aws": "projected"}


async def test_non_agentcore_owner_projects_nothing(wired, monkeypatch):
    monkeypatch.setattr(mod.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=False))
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("github")))

    assert await mod.reproject_connections(OWNER) == {}

    mod.hermes_cli.project.assert_not_awaited()


async def test_dry_run_decides_without_writing(wired, monkeypatch):
    monkeypatch.setattr(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=_rows("github", "meta_ads")))
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value={"token": "t"}))

    assert await mod.reproject_connections(OWNER, dry_run=True) == {"github": "projected", "meta_ads": "projected"}

    mod.hermes_cli.project.assert_not_awaited()
    wired.assert_not_awaited()
