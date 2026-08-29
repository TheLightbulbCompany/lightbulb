import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_landing as mod  # noqa: E402

RES = {
    "resource_id": "res_1",
    "provider": "posthog",
    "service": "analytics",
    "status": "active",
    "resource_name": "Northwind analytics",
    "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    "dashboard_url": "https://us.posthog.com/project/1",
    "level": "company",
}


@pytest.mark.asyncio
async def test_land_writes_secrets_manager_then_the_row_then_the_container():
    order: list[str] = []
    with (
        # Patched even though the brief's version leaves it real: unpatched it
        # makes a live Secrets Manager call, which passes only on a machine
        # with AWS creds and raises NoCredentialsError in CI.
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(
            mod.integration_secret_store, "put", AsyncMock(side_effect=lambda *a, **k: order.append("sm"))
        ) as sm_put,
        patch.object(
            mod.integrations_repo,
            "record_provider_resource",
            AsyncMock(side_effect=lambda *a, **k: order.append("row") or []),
        ) as record,
        patch.object(
            mod.config_patcher, "set_openclaw_config_path", AsyncMock(side_effect=lambda *a, **k: order.append("cfg"))
        ) as patch_cfg,
    ):
        await mod.land_credentials(
            owner_id="org_a", resource=RES, env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"}
        )
    assert order == ["sm", "row", "cfg"]
    sm_put.assert_awaited_once_with("org_a", "posthog", {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"})
    assert record.await_args.args[:2] == ("org_a", "posthog")
    assert record.await_args.args[2]["resource_id"] == "res_1"
    patch_cfg.assert_awaited_once_with("org_a", ["env", "vars", "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"], "phc_live_abc")


@pytest.mark.asyncio
async def test_land_merges_new_env_with_what_the_provider_already_had():
    """A second resource for the same provider must not blank the first one's
    vars — the Secrets Manager blob is per-(owner, provider)."""
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"OLD_KEY": "old"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])),
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES, env={"NEW_KEY": "new"})
    assert sm_put.await_args.args[2] == {"OLD_KEY": "old", "NEW_KEY": "new"}


@pytest.mark.asyncio
async def test_land_never_puts_a_secret_value_in_dynamodb():
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()),
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])) as record,
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(
            owner_id="org_a", resource=RES, env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_secret"}
        )
    assert "phc_secret" not in repr(record.await_args.args[2])
    assert record.await_args.args[2]["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]


@pytest.mark.asyncio
async def test_build_provisioned_env_reads_only_rows_with_the_projects_actor():
    rows = [
        {"provider": "posthog", "actor": "projects", "status": "connected"},
        {"provider": "linear", "status": "connected"},  # a normal integration, not ours
        {"provider": "vercel", "actor": "projects", "status": "disconnected"},
    ]
    with (
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(
            mod.integration_secret_store, "get", AsyncMock(side_effect=[{"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}])
        ) as sm_get,
    ):
        env = await mod.build_provisioned_env("org_a")
    assert env == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}
    assert sm_get.await_count == 1


@pytest.mark.asyncio
async def test_build_provisioned_env_never_fails_the_caller():
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await mod.build_provisioned_env("org_a") == {}


@pytest.mark.asyncio
async def test_summary_pairs_state_with_pointers():
    rows = [
        {"provider": "posthog", "actor": "projects", "provider_resources": [RES]},
        {"provider": "linear"},
    ]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)):
        state, resources = await mod.provisioning_summary("org_a")
    assert state == "paired"
    assert [r["resource_id"] for r in resources] == ["res_1"]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[{"provider": "linear"}])):
        assert await mod.provisioning_summary("org_a") == ("unpaired", [])
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await mod.provisioning_summary("org_a") == ("unpaired", [])
