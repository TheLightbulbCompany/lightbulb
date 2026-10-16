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


POINTER = {
    "resource_id": "res-1",
    "provider": "resend",
    "service": "email",
    "resource_name": "Acme mail",
    "status": "active",
    "env_keys": ["RESEND_API_KEY"],
    "dashboard_url": None,
    "level": "company",
    "workflow_id": "wf-1",
    "project_id": "proj-1",
}


@pytest.fixture(autouse=True)
def _no_profiles(monkeypatch):
    mod._provider_locks.clear()  # locks bind to the test's event loop
    monkeypatch.setattr(mod, "_roster", AsyncMock(return_value=[]))


@pytest.fixture
def stores(monkeypatch):
    secret = {}
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(side_effect=lambda o, k: secret.get((o, k))))
    monkeypatch.setattr(
        mod.integration_secret_store,
        "put",
        AsyncMock(side_effect=lambda o, k, blob: secret.__setitem__((o, k), dict(blob)) or "arn"),
    )
    pointer = AsyncMock(return_value=[POINTER])
    monkeypatch.setattr(mod.integrations_repo, "record_provider_resource", pointer)
    patched = []
    monkeypatch.setattr(mod, "_roster", AsyncMock(return_value=["agent-a", "agent-b"]))
    monkeypatch.setattr(mod, "_patch_profile_env", lambda o, a, env: patched.append((a, dict(env))))
    return secret, pointer, patched


@pytest.mark.asyncio
async def test_landing_writes_secret_pointer_and_every_profile_env(stores):
    secret, pointer, patched = stores
    await mod.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret[("o1", mod.integrations_repo.projects_key("resend"))] == {"RESEND_API_KEY": "re_123"}
    pointer.assert_awaited_once_with("o1", "resend", POINTER)
    assert patched == [("agent-a", {"RESEND_API_KEY": "re_123"}), ("agent-b", {"RESEND_API_KEY": "re_123"})]


@pytest.mark.asyncio
async def test_landing_merges_into_the_providers_existing_blob(stores):
    secret, _, _ = stores
    secret[("o1", mod.integrations_repo.projects_key("resend"))] = {"RESEND_OTHER": "x"}
    await mod.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret[("o1", mod.integrations_repo.projects_key("resend"))] == {
        "RESEND_OTHER": "x",
        "RESEND_API_KEY": "re_123",
    }


@pytest.mark.asyncio
async def test_concurrent_landings_for_one_provider_keep_both_keys(stores, monkeypatch):
    import asyncio

    secret, _, _ = stores
    real_get = mod.integration_secret_store.get

    async def slow_get(o, k):
        blob = await real_get(o, k)
        await asyncio.sleep(0)  # yield between read and write, as a real store would
        return blob

    monkeypatch.setattr(mod.integration_secret_store, "get", slow_get)
    await asyncio.gather(
        mod.land_credentials(owner_id="o1", resource=POINTER, env={"A_KEY": "a"}),
        mod.land_credentials(owner_id="o1", resource=POINTER, env={"B_KEY": "b"}),
    )
    assert secret[("o1", mod.integrations_repo.projects_key("resend"))] == {"A_KEY": "a", "B_KEY": "b"}


@pytest.mark.asyncio
async def test_landing_with_no_env_still_records_the_pointer_and_touches_no_profile(stores):
    secret, pointer, patched = stores
    await mod.land_credentials(owner_id="o1", resource={**POINTER, "env_keys": []}, env={})
    assert not secret and patched == [] and pointer.await_count == 1


@pytest.mark.asyncio
async def test_a_profile_write_failure_is_raised_after_the_secret_and_pointer_landed(stores, monkeypatch):
    secret, pointer, _ = stores
    monkeypatch.setattr(mod, "_patch_profile_env", lambda o, a, env: (_ for _ in ()).throw(FileNotFoundError(a)))
    with pytest.raises(FileNotFoundError):
        await mod.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_123"})
    assert secret and pointer.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["PAPERCLIP_API_KEY", "AWS_SECRET_ACCESS_KEY", "OPENROUTER_API_KEY"])
async def test_a_reserved_env_name_is_refused_before_any_write(stores, name):
    from core.services.provisioning_workflow import WorkflowRefused

    secret, pointer, patched = stores
    with pytest.raises(WorkflowRefused) as refused:
        await mod.land_credentials(owner_id="o1", resource=POINTER, env={"RESEND_API_KEY": "re_1", name: "x"})
    assert refused.value.code == "reserved_env_name"
    assert not secret and pointer.await_count == 0 and patched == []
    mod.integration_secret_store.get.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_revoke_clears_the_revoked_keys_from_every_profile_but_not_a_survivors(stores, monkeypatch):
    secret, _, patched = stores
    first = {**POINTER, "env_keys": ["RESEND_API_KEY", "RESEND_SHARED"]}
    survivor = {**POINTER, "resource_id": "res-2", "env_keys": ["RESEND_SHARED"]}
    await mod.land_credentials(owner_id="o1", resource=first, env={"RESEND_API_KEY": "re_1", "RESEND_SHARED": "s"})
    monkeypatch.setattr(mod.integrations_repo, "list_provider_resources", AsyncMock(return_value=[first, survivor]))
    monkeypatch.setattr(mod.integrations_repo, "remove_provider_resource", AsyncMock())
    patched.clear()
    assert await mod.revoke_credentials(owner_id="o1", provider="resend", resource_id="res-1")
    assert patched == [("agent-a", {"RESEND_API_KEY": ""}), ("agent-b", {"RESEND_API_KEY": ""})]


@pytest.mark.asyncio
async def test_a_landing_during_a_revoke_keeps_its_value(stores, monkeypatch):
    import asyncio

    secret, _, patched = stores
    key = mod.integrations_repo.projects_key("resend")
    secret[("o1", key)] = {"K": "old"}
    rows = [{"resource_id": "res-old", "provider": "resend", "env_keys": ["K"]}]
    new = {**POINTER, "resource_id": "res-new", "env_keys": ["K"]}

    async def slow_list(o, p):
        snapshot = list(rows)
        await asyncio.sleep(0)  # a landing could slip in between snapshot and trim
        return snapshot

    monkeypatch.setattr(mod.integrations_repo, "list_provider_resources", slow_list)
    monkeypatch.setattr(
        mod.integration_secret_store, "delete", AsyncMock(side_effect=lambda o, k: secret.pop((o, k), None))
    )
    monkeypatch.setattr(
        mod.integrations_repo, "record_provider_resource", AsyncMock(side_effect=lambda o, p, r: rows.append(r))
    )
    monkeypatch.setattr(
        mod.integrations_repo,
        "remove_provider_resource",
        AsyncMock(
            side_effect=lambda o, p, rid: (
                rows[:] and rows.__setitem__(slice(None), [r for r in rows if r["resource_id"] != rid])
            )
        ),
    )
    await asyncio.gather(
        mod.revoke_credentials(owner_id="o1", provider="resend", resource_id="res-old"),
        mod.land_credentials(owner_id="o1", resource=new, env={"K": "new"}),
    )
    assert secret[("o1", key)] == {"K": "new"}
    assert patched[-1][1] == {"K": "new"}
    assert [r["resource_id"] for r in rows] == ["res-new"]
