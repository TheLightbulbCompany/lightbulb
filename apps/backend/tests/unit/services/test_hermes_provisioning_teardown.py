"""F5 (predeploy review, M6-R38) -- ``hermes_provisioning.teardown_hermes_runtime``, extracted
from ``migrate_workspace_to_hermes.rollback``'s own AgentCore half so an account wipe on an
AgentCore row (``ecs_manager.delete_user_service``) shares the exact same teardown instead of
leaking the runtime/secret/S3 profiles the rollback path already knew to clean up.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning

ROW = {
    "owner_id": "org_a",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
    "bedrock_gate_token_hash": "hash-1",
}


@pytest.fixture(autouse=True)
def no_browser_state(monkeypatch):
    monkeypatch.setattr("core.repositories.container_repo.get_browser_state", AsyncMock(return_value=None))


def test_delete_profile_prefix_objects_paginates_all_owner_scoped_prefixes(monkeypatch):
    class Paginator:
        def __init__(self):
            self.prefixes = []

        def paginate(self, *, Bucket, Prefix):
            assert Bucket == "test-bucket"
            self.prefixes.append(Prefix)
            if Prefix == "org_a/hermes/":
                return iter(
                    [
                        {"Contents": [{"Key": "org_a/hermes/agent_a/config.yaml"}]},
                        {"Contents": [{"Key": "org_a/hermes/auth.json"}]},
                    ]
                )
            if Prefix == "marketplace-installs/org_a/":
                return iter([{"Contents": [{"Key": "marketplace-installs/org_a/agent_a.json"}]}])
            if Prefix == "browser-state/org_a/":
                return iter([{"Contents": [{"Key": "browser-state/org_a/checkpoint.json"}]}])
            if Prefix == "org_a/browser-extensions/":
                return iter([{"Contents": [{"Key": "org_a/browser-extensions/supademo.zip"}]}])
            raise AssertionError(f"unexpected prefix: {Prefix}")

    class Client:
        def __init__(self):
            self.paginator = Paginator()
            self.deleted = []

        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return self.paginator

        def delete_objects(self, *, Bucket, Delete):
            self.deleted.extend((Bucket, item["Key"]) for item in Delete["Objects"])

    client = Client()
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "test-bucket")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda _name: client))

    hermes_provisioning._delete_profile_prefix_objects("org_a")

    assert client.paginator.prefixes == [
        "org_a/hermes/",
        "marketplace-installs/org_a/",
        "browser-state/org_a/",
        "org_a/browser-extensions/",
    ]
    assert client.deleted == [
        ("test-bucket", "org_a/hermes/agent_a/config.yaml"),
        ("test-bucket", "org_a/hermes/auth.json"),
        ("test-bucket", "marketplace-installs/org_a/agent_a.json"),
        ("test-bucket", "browser-state/org_a/checkpoint.json"),
        ("test-bucket", "org_a/browser-extensions/supademo.zip"),
    ]
    assert all("org_b" not in key for _, key in client.deleted)


@pytest.mark.asyncio
async def test_teardown_stops_session_deletes_runtime_revokes_hash_and_deletes_the_secret_in_order(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: calls.append("stop_session"))
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: calls.append("delete_runtime"))
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: calls.append("delete_secret"))
    delete_hashes = AsyncMock(side_effect=lambda hashes: calls.append("revoke_token"))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", delete_hashes)
    delete_profiles = AsyncMock(side_effect=AssertionError("must not delete S3 profiles on a rollback"))
    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", delete_profiles)

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=False)

    assert calls == ["stop_session", "delete_runtime", "revoke_token", "delete_secret"]
    delete_hashes.assert_awaited_once_with(["hash-1"])
    delete_profiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_tolerates_a_missing_runtime_and_hash(monkeypatch):
    """No ARN and no gate-token hash on the row (e.g. torn down right after prepare, before
    create) -- nothing to stop/delete/revoke on those fronts, but the boot secret is still
    force-deleted."""
    stop_session = AsyncMock(side_effect=AssertionError("nothing to stop"))
    delete_runtime = AsyncMock(side_effect=AssertionError("nothing to delete"))
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", stop_session)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", delete_runtime)
    delete_hashes = AsyncMock(side_effect=AssertionError("no hash to revoke"))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", delete_hashes)
    deleted_secret = {}
    monkeypatch.setattr(
        hermes_provisioning, "_delete_boot_secret", lambda owner_id: deleted_secret.setdefault("owner_id", owner_id)
    )

    await hermes_provisioning.teardown_hermes_runtime(
        "org_a", container_row={"owner_id": "org_a"}, delete_profiles=False
    )

    stop_session.assert_not_called()
    delete_runtime.assert_not_called()
    delete_hashes.assert_not_called()
    assert deleted_secret["owner_id"] == "org_a"


@pytest.mark.asyncio
async def test_teardown_with_delete_profiles_true_also_deletes_the_s3_prefix(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())
    deleted = {}
    monkeypatch.setattr(
        hermes_provisioning, "_delete_profile_prefix_objects", lambda owner_id: deleted.setdefault("owner_id", owner_id)
    )

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=True)

    assert deleted["owner_id"] == "org_a"


@pytest.mark.asyncio
async def test_teardown_is_best_effort_when_a_step_raises(monkeypatch):
    """An already-cold session, an already-gone runtime, a stranded gate-token row, or a
    transient Secrets Manager error must never stop the rest of the teardown from running."""
    calls: list[str] = []

    def _raise_stop(*a):
        calls.append("stop_session")
        raise RuntimeError("unknown session")

    def _raise_delete(*a):
        calls.append("delete_runtime")
        raise RuntimeError("already gone")

    async def _raise_revoke(hashes):
        calls.append("revoke_token")
        raise RuntimeError("ddb down")

    def _record_secret(owner_id):
        calls.append("delete_secret")

    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", _raise_stop)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", _raise_delete)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", _raise_revoke)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", _record_secret)

    await hermes_provisioning.teardown_hermes_runtime(
        "org_a", container_row=ROW, delete_profiles=False
    )  # must not raise

    assert calls == ["stop_session", "delete_runtime", "revoke_token", "delete_secret"]


@pytest.mark.asyncio
async def test_teardown_s3_delete_failure_is_also_best_effort(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())

    def _raise(owner_id):
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", _raise)

    # Must not raise.
    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=True)


def _seam(monkeypatch, *, row):
    """delete_workspace_runtime with every collaborator stubbed; returns the call log."""
    calls: list[str] = []
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr("core.repositories.container_repo.delete", AsyncMock(return_value=row))
    monkeypatch.setattr("core.repositories.container_repo.delete_browser_state", AsyncMock())
    monkeypatch.setattr(hermes_provisioning, "teardown_hermes_runtime", AsyncMock())
    monkeypatch.setattr(hermes_provisioning, "_delete_profile_prefix_objects", lambda *a: None)
    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.pause_owner_automation", AsyncMock())

    async def _reap(*a, **kw):
        calls.append("reap")
        return {}

    monkeypatch.setattr("core.billing.bedrock_aip.reap_owner_bedrock_resources", _reap)
    monkeypatch.setattr("core.repositories.channel_link_repo.sweep_by_owner", AsyncMock(return_value=0))

    async def _wipe(owner_id):
        calls.append(f"memory:{owner_id}")

    monkeypatch.setattr("core.services.workspace_memory.delete_owner_memory", _wipe)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [ROW, None], ids=["with-row", "row-less-retry"])
async def test_the_seam_wipes_workspace_memory_with_the_profiles_even_on_a_row_less_retry(monkeypatch, row):
    """Workspace memory is the owner's data like the profiles are. A retried wipe arrives with the
    row already gone, so the memory wipe cannot live inside the ``if container`` branch. It runs
    AFTER the reap, which needs this call's deleted-row image."""
    calls = _seam(monkeypatch, row=row)
    await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)
    assert calls == ["reap", "memory:org_a"]


@pytest.mark.asyncio
async def test_a_reprovision_keeps_workspace_memory(monkeypatch):
    calls = _seam(monkeypatch, row=ROW)
    await hermes_provisioning.delete_workspace_runtime("org_a")
    assert "memory:org_a" not in calls


@pytest.mark.asyncio
async def test_a_failed_memory_wipe_fails_the_seam(monkeypatch):
    """Records never expire: 'teardown incomplete, retry me', like a partial reap."""
    _seam(monkeypatch, row=ROW)
    monkeypatch.setattr(
        "core.services.workspace_memory.delete_owner_memory", AsyncMock(side_effect=RuntimeError("throttled"))
    )
    with pytest.raises(RuntimeError, match="throttled"):
        await hermes_provisioning.delete_workspace_runtime("org_a", delete_profiles=True)


@pytest.mark.asyncio
async def test_teardown_pauses_paperclip_automation_and_a_paperclip_failure_never_blocks_it(monkeypatch):
    """No runtime = every scheduled routine/heartbeat files a task that can only block."""
    calls = _seam(monkeypatch, row=ROW)
    pause = AsyncMock(side_effect=RuntimeError("paperclip down"))
    monkeypatch.setattr("core.services.paperclip_agent_lifecycle.pause_owner_automation", pause)
    await hermes_provisioning.delete_workspace_runtime("org_a")
    pause.assert_awaited_once_with("org_a")
    assert calls == ["reap"]


@pytest.mark.asyncio
async def test_teardown_deletes_network_state_even_without_delete_profiles(monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda *a: None)
    monkeypatch.setattr(hermes_provisioning, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.delete_gate_token_rows", AsyncMock())
    deleted: list[list[str]] = []
    monkeypatch.setattr(hermes_provisioning, "_delete_prefixes", lambda prefixes: deleted.append(prefixes))

    await hermes_provisioning.teardown_hermes_runtime("org_a", container_row=ROW, delete_profiles=False)

    assert deleted == [["org_a/network/"]]
