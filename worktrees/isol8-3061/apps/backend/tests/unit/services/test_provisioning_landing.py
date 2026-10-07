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
async def test_land_refuses_unconditionally_before_any_write():
    """The landing was a locked RMW into ``openclaw.json["env"]["vars"]``, and the profile
    ``.env``/secret-scope equivalent is deliberately out of scope. Refusing the WHOLE landing --
    rather than persisting the secret and the pointer and then failing to deliver -- is what
    keeps a workflow from going "active" over a credential nothing can reach.

    It used to refuse only for an AgentCore owner; there is one runtime, so it always refuses
    and nothing is written first."""
    from core.services.provisioning_workflow import WorkflowRefused

    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock()) as record,
    ):
        with pytest.raises(WorkflowRefused) as exc:
            await mod.land_credentials(owner_id="org_a", resource=RES, env={"X": "1"})
    assert exc.value.code == "unsupported_on_agentcore"
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    record.assert_not_awaited()


REVOKE_RES = {"resource_id": "res_1", "provider": "posthog", "env_keys": ["K"]}


@pytest.mark.asyncio
async def test_revoke_drops_only_the_landed_keys_then_config_then_the_pointer():
    """Revoke drops only the keys THIS resource landed, never the whole blob
    (one provider's blob holds every resource it landed) — and the pointer
    removal happens LAST so a retry after a config failure can still find the
    keys. The secret is addressed by the NAMESPACED key.

    The config strip that used to sit between the two (the plaintext copy in the owner's
    ``openclaw.json`` ``env.vars``) is gone with the file -- Secrets Manager holds the only
    copy there is."""
    order: list[str] = []
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"OTHER": "t", "K": "p"})),
        patch.object(
            mod.integration_secret_store, "put", AsyncMock(side_effect=lambda *a, **k: order.append("put"))
        ) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
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
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")
    assert order == ["put", "pointer"]


@pytest.mark.asyncio
async def test_revoke_deletes_the_secret_when_nothing_is_left():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"K": "p"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
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
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])),
    ):
        assert await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_a") is True
    sm_put.assert_awaited_once_with("org_a", "projects/posthog", {"K": "shared"})
    sm_del.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_with_no_env_keys_only_removes_the_pointer():
    res_no_keys = {"resource_id": "res_2", "provider": "posthog", "env_keys": []}
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[res_no_keys])),
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_2")
    assert result is True
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
    drop.assert_awaited_once_with("org_a", "posthog", "res_2")


@pytest.mark.asyncio
async def test_revoke_missing_pointer_is_a_pure_no_op():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[])),
        patch.object(mod.integration_secret_store, "get", AsyncMock()) as sm_get,
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock()) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_missing")
    assert result is False
    sm_get.assert_not_awaited()
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
    drop.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_skips_the_secret_step_when_the_blob_is_already_gone():
    with (
        patch.object(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[REVOKE_RES])),
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
    ):
        result = await mod.revoke_credentials(owner_id="org_a", provider="posthog", resource_id="res_1")
    assert result is True
    sm_put.assert_not_awaited()
    sm_del.assert_not_awaited()
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
