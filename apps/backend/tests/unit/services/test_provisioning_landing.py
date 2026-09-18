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
    # NAMESPACED secret key: the bare (owner, "posthog") blob belongs to the
    # OAuth rail. The repo call keeps the BARE provider — it namespaces the
    # sort key itself.
    sm_put.assert_awaited_once_with("org_a", "projects/posthog", {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"})
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
async def test_land_merges_the_blob_verbatim_including_non_string_fields():
    """Merged with no type filter: the store documents a round-trip of the exact
    dict, so a filter would silently drop any non-string field a landing adds."""
    blob = {"a_token": "t", "expires_in": 3600, "meta": None}
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=blob)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])),
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES, env={"X": "1"})
    assert sm_put.await_args.args[2] == {
        "a_token": "t",
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
async def test_land_refuses_on_agentcore_before_any_write():
    """The locked RMW into openclaw.json["env"]["vars"] is a dead write on
    AgentCore -- there is no delivery mechanism yet (that is the profile
    .env/secret-scope mechanism, out of scope here). Refuse the whole landing
    rather than silently mark the workflow active with nothing actually
    reachable by the agent."""
    from core.services.provisioning_workflow import WorkflowRefused

    with (
        patch.object(mod.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True)) as is_ac,
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock()) as record,
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()) as patch_cfg,
    ):
        with pytest.raises(WorkflowRefused) as exc:
            await mod.land_credentials(owner_id="org_a", resource=RES, env={"X": "1"})
    assert exc.value.code == "unsupported_on_agentcore"
    is_ac.assert_awaited_once_with("org_a")
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    record.assert_not_awaited()
    patch_cfg.assert_not_awaited()


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
    """Revoke drops only the keys THIS resource landed, never the whole blob
    (one provider's blob holds every resource it landed) — and the pointer
    removal happens LAST so a retry after a config failure can still find the
    keys. The secret is addressed by the NAMESPACED key."""
    order: list[str] = []
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"OTHER": "t", "K": "p"})),
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
    sm_put.assert_awaited_once_with("org_a", "projects/posthog", {"OTHER": "t"})
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
    sm_del.assert_awaited_once_with("org_a", "projects/posthog")


@pytest.mark.asyncio
async def test_revoke_keeps_an_env_name_a_sibling_resource_still_declares():
    """Two resources of the same provider can land the same variable name.
    Revoking one must not strip a name the other still declares — neither from
    the blob nor from the container."""
    a = {"resource_id": "res_a", "provider": "posthog", "env_keys": ["K", "ONLY_A"]}
    b = {"resource_id": "res_b", "provider": "posthog", "env_keys": ["K"]}
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[a, b])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"K": "shared", "ONLY_A": "x"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()) as del_cfg,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])),
    ):
        assert await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_a") is True
    sm_put.assert_awaited_once_with("org_a", "projects/posthog", {"K": "shared"})
    sm_del.assert_not_awaited()
    del_cfg.assert_awaited_once_with("org_a", ["env", "vars", "ONLY_A"])


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
async def test_revoke_still_strips_openclaw_json_for_a_migrated_agentcore_owner():
    """The config strip is a pure EFS locked-RMW file op -- it never talks to
    the container, so there is no AgentCore RPC cost to avoid. A workspace
    that migrated ECS->AgentCore keeps its EFS tree (migration doesn't delete
    it), and that stale openclaw.json can still hold the plaintext secret
    value: revoke must scrub it there too, exactly like on ECS. Skipping this
    write would leave the plaintext resident indefinitely."""
    with (
        patch.object(mod.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True)) as is_ac,
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"OTHER": "t", "K": "p"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.config_patcher, "delete_openclaw_config_path", AsyncMock()) as del_cfg,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    is_ac.assert_not_awaited()  # revoke's config strip never branches on runtime host
    sm_put.assert_awaited_once_with("org_a", "projects/posthog", {"OTHER": "t"})
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
    sm_del.assert_awaited_once_with("org_a", "projects/posthog")
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")


@pytest.mark.asyncio
async def test_build_provisioned_env_reads_only_namespaced_rows_with_the_projects_actor():
    # Every row carries env_keys so that the PREFIX and ACTOR filters are what
    # exclude the last two — not an incidentally empty key list.
    keys = [{"resource_id": "r", "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]}]
    rows = [
        {"provider": "projects/posthog", "actor": "projects", "provider_resources": keys},
        # the OAuth rail's own row for the SAME provider — bare key, never ours
        {"provider": "posthog", "provider_resources": keys},
        # actor stamped but not namespaced: still not ours
        {"provider": "vercel", "actor": "projects", "provider_resources": keys},
    ]
    with (
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(
            mod.integration_secret_store, "get", AsyncMock(side_effect=[{"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}])
        ) as sm_get,
    ):
        env = await mod.build_provisioned_env("org_a")
    assert env == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}
    # Read from the NAMESPACED blob, never the OAuth one.
    sm_get.assert_awaited_once_with("org_a", "projects/posthog")


@pytest.mark.asyncio
async def test_build_provisioned_env_injects_only_the_names_the_row_declares():
    """openclaw.json gets exactly the variables the CLI landed — nothing else in
    the blob leaks into the container's environment."""
    rows = [
        {
            "provider": "projects/posthog",
            "actor": "projects",
            "provider_resources": [RES],
        }
    ]
    blob = {
        "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc",
        "some_other_field": "not_an_env_var",
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
async def test_summary_returns_only_this_rails_pointers():
    rows = [
        {"provider": "projects/posthog", "actor": "projects", "provider_resources": [RES]},
        {"provider": "linear"},
    ]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)):
        resources = await mod.provisioning_summary("org_a")
    assert [r["resource_id"] for r in resources] == ["res_1"]
    # The POINTER carries the bare provider — that is what /resources renders
    # and what the revoke route takes back.
    assert [r["provider"] for r in resources] == ["posthog"]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[{"provider": "linear"}])):
        assert await mod.provisioning_summary("org_a") == []
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await mod.provisioning_summary("org_a") == []
