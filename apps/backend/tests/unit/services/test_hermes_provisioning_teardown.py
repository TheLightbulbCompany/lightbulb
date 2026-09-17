"""F5 (predeploy review, M6-R38) -- ``hermes_provisioning.teardown_hermes_runtime``, extracted
from ``migrate_workspace_to_hermes.rollback``'s own AgentCore half so an account wipe on an
AgentCore row (``ecs_manager.delete_user_service``) shares the exact same teardown instead of
leaking the runtime/secret/S3 profiles the rollback path already knew to clean up.
"""

from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning

ROW = {
    "owner_id": "org_a",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
    "bedrock_gate_token_hash": "hash-1",
}


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
