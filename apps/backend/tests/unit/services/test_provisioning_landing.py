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
async def test_land_merges_the_blob_verbatim_including_non_string_oauth_fields():
    """The (owner, provider) blob is SHARED with the OAuth rail — `stripe` and
    `posthog` are registry providers too. Type-filtering the merge would drop
    `expires_in: int | None` and break the store's documented round-trip."""
    oauth_blob = {"access_token": "t", "expires_in": 3600, "meta": None}
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=oauth_blob)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])),
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES, env={"X": "1"})
    assert sm_put.await_args.args[2] == {
        "access_token": "t",
        "expires_in": 3600,
        "meta": None,
        "X": "1",
    }


@pytest.mark.asyncio
async def test_land_survives_a_container_with_no_config_yet():
    """No openclaw.json means the container is not provisioned. The credential is
    already durable and build_provisioned_env injects it at provision, so the
    landing must not fail."""
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])) as record,
        patch.object(
            mod.config_patcher,
            "set_openclaw_config_path",
            AsyncMock(side_effect=mod.ConfigPatchError("no config")),
        ),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES, env={"X": "1"})
    sm_put.assert_awaited_once()
    record.assert_awaited_once()


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
    # The row gets the caller's pointer dict itself — names, never values.
    assert record.await_args.args[2] is RES
    assert record.await_args.args[2]["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]


REVOKE_RES = {"resource_id": "res_1", "provider": "posthog", "env_keys": ["K"]}


@pytest.mark.asyncio
async def test_revoke_drops_only_the_landed_keys_then_config_then_the_pointer():
    """The (owner, provider) blob is SHARED with the OAuth rail, so revoke must
    drop only the keys THIS resource landed, never the whole blob — and the
    pointer removal happens LAST so a retry after a config failure can still
    find the keys."""
    order: list[str] = []
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"access_token": "t", "K": "p"})),
        patch.object(
            mod.integration_secret_store, "put", AsyncMock(side_effect=lambda *a, **k: order.append("put"))
        ) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(
            mod.config_patcher,
            "delete_openclaw_config_path",
            AsyncMock(side_effect=lambda *a, **k: order.append("cfg")),
        ) as del_cfg,
        patch.object(
            mod.integrations_repo,
            "remove_provider_resource",
            AsyncMock(side_effect=lambda *a, **k: order.append("pointer") or []),
        ) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    sm_put.assert_awaited_once_with("org_a", "posthog", {"access_token": "t"})
    sm_del.assert_not_awaited()
    del_cfg.assert_awaited_once_with("org_a", ["env", "vars", "K"])
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")
    assert order == ["put", "cfg", "pointer"]


@pytest.mark.asyncio
async def test_revoke_deletes_the_secret_when_nothing_is_left():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"K": "p"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()),
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])),
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    sm_put.assert_not_awaited()
    sm_del.assert_awaited_once_with("org_a", "posthog")


@pytest.mark.asyncio
async def test_revoke_with_no_env_keys_only_removes_the_pointer():
    res_no_keys = {"resource_id": "res_2", "provider": "posthog", "env_keys": []}
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[res_no_keys])),
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()) as del_cfg,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_2")
    assert result is True
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
    del_cfg.assert_not_awaited()
    drop.assert_awaited_once_with("org_a", "posthog", "res_2")


@pytest.mark.asyncio
async def test_revoke_missing_pointer_is_a_pure_no_op():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()) as del_cfg,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock()) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_missing")
    assert result is False
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
    del_cfg.assert_not_awaited()
    drop.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_skips_the_secret_step_when_the_blob_is_already_gone():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()) as del_cfg,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
    del_cfg.assert_awaited_once_with("org_a", ["env", "vars", "K"])
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")


@pytest.mark.asyncio
async def test_revoke_swallows_a_missing_container_config():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"K": "p"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()),
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(
            mod.config_patcher,
            "delete_openclaw_config_path",
            AsyncMock(side_effect=mod.ConfigPatchError("no config")),
        ),
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    sm_del.assert_awaited_once_with("org_a", "posthog")
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")


@pytest.mark.asyncio
async def test_build_provisioned_env_reads_only_rows_with_the_projects_actor():
    # Every row carries env_keys so that the ACTOR and STATUS filters are what
    # exclude the last two — not an incidentally empty key list.
    keys = [{"resource_id": "r", "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]}]
    rows = [
        {"provider": "posthog", "actor": "projects", "status": "connected", "provider_resources": keys},
        # a normal integration, not ours
        {"provider": "linear", "status": "connected", "provider_resources": keys},
        {"provider": "vercel", "actor": "projects", "status": "disconnected", "provider_resources": keys},
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
async def test_build_provisioned_env_injects_only_the_names_the_row_declares():
    """The blob is shared with the OAuth rail, so injecting all of it would put
    access_token / refresh_token / client_secret into openclaw.json for any
    provider connected on both rails."""
    rows = [
        {
            "provider": "posthog",
            "actor": "projects",
            "status": "connected",
            "provider_resources": [RES],
        }
    ]
    blob = {
        "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc",
        "access_token": "oauth_secret",
        "refresh_token": "oauth_refresh",
    }
    with (
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=blob)),
    ):
        env = await mod.build_provisioned_env("org_a")
    assert env == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}


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
