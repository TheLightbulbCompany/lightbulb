"""Scoped migration repair: dry-run, exact context edit, model pins, and spend fence."""

from unittest.mock import AsyncMock

import pytest
import yaml

from scripts import repair_hermes_migration as repair


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trial", "credentials"])
async def test_repair_is_dry_run_by_default_and_preserves_other_content(monkeypatch, mode):
    arns = {"sonnet": "arn:sonnet", "opus": "arn:opus", "haiku": "arn:haiku"}
    monkeypatch.setattr(
        repair.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": "runtime", "bedrock_aip_arns": arns}),
    )
    monkeypatch.setattr(
        repair.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "trial_local"})
    )
    monkeypatch.setattr(repair, "load_agent_roster", AsyncMock(return_value=[{"id": "agent"}]))
    reconcile = AsyncMock()
    monkeypatch.setattr(repair, "reconcile_hermes_aips", reconcile)
    monkeypatch.setattr(repair.profiles.settings, "OWNER_STATE_BUCKET", "bucket")
    config = {
        "model": {"default": "arn:opus", "provider": "bedrock"},
        "auxiliary": {"compression": {"model": "arn:haiku"}},
        "user_setting": "keep",
    }
    original = (
        yaml.safe_dump(config)
        if mode == "trial"
        else "owner instructions\n" + repair.LEGACY_EXAMPLE + "\nkeep this too"
    )
    store = {"text": original}
    writes = []
    monkeypatch.setattr(repair.profiles, "_s3_get_versioned", lambda *_: (store["text"], '"etag"'))

    def put(bucket, key, body, etag):
        assert etag == '"etag"'
        writes.append(key)
        store["text"] = body

    monkeypatch.setattr(repair.profiles, "_s3_put_conditionally", put)
    monkeypatch.setattr(repair.profiles, "_s3_put", lambda bucket, key, body: writes.append(key))
    await repair.repair_owner("org", mode)
    assert not writes
    reconcile.assert_not_awaited()
    await repair.repair_owner("org", mode, apply=True)
    assert writes[-1].endswith(".revision")
    if mode == "trial":
        result = yaml.safe_load(store["text"])
        assert result["user_setting"] == "keep"
        assert result["model"]["default"] == "arn:sonnet"
        for task in repair.profiles.AUXILIARY_TASKS:
            assert result["auxiliary"][task]["model"] == "arn:sonnet"
            assert result["auxiliary"][task]["fallback_chain"] == []
        reconcile.assert_awaited_once_with("org")
    else:
        assert store["text"] == "owner instructions\n" + repair.CREDENTIAL_REFERENCE + "\nkeep this too"
        reconcile.assert_not_awaited()
    writes.clear()
    await repair.repair_owner("org", mode, apply=True)
    assert len(writes) == 1 and writes[0].endswith(".revision")


@pytest.mark.asyncio
async def test_failed_profile_write_still_narrows_trial_fence(monkeypatch):
    monkeypatch.setattr(
        repair.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": "runtime", "bedrock_aip_arns": {"sonnet": "arn:sonnet"}}),
    )
    monkeypatch.setattr(
        repair.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "trial_local"})
    )
    monkeypatch.setattr(repair, "load_agent_roster", AsyncMock(return_value=[{"id": "agent"}]))
    monkeypatch.setattr(repair.profiles.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(repair.profiles, "_s3_get_versioned", lambda *_: ("model: {}", '"etag"'))

    def conflict(*_):
        raise RuntimeError("conditional write conflict")

    monkeypatch.setattr(repair.profiles, "_s3_put_conditionally", conflict)
    reconcile = AsyncMock()
    monkeypatch.setattr(repair, "reconcile_hermes_aips", reconcile)
    with pytest.raises(RuntimeError, match="conditional write conflict"):
        await repair.repair_owner("org", "trial", apply=True)
    reconcile.assert_awaited_once_with("org")


@pytest.mark.asyncio
async def test_revision_failure_is_healed_on_retry(monkeypatch):
    monkeypatch.setattr(
        repair.container_repo, "get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": "runtime"})
    )
    monkeypatch.setattr(repair, "load_agent_roster", AsyncMock(return_value=[{"id": "agent"}]))
    monkeypatch.setattr(repair.profiles.settings, "OWNER_STATE_BUCKET", "bucket")
    store = {"body": repair.LEGACY_EXAMPLE}
    monkeypatch.setattr(repair.profiles, "_s3_get_versioned", lambda *_: (store["body"], '"etag"'))
    monkeypatch.setattr(
        repair.profiles, "_s3_put_conditionally", lambda bucket, key, body, etag: store.update(body=body)
    )

    def fail_revision(*_):
        raise RuntimeError("revision failed")

    monkeypatch.setattr(repair.profiles, "_s3_put", fail_revision)
    with pytest.raises(RuntimeError, match="revision failed"):
        await repair.repair_owner("org", "credentials", apply=True)
    assert store["body"] == repair.CREDENTIAL_REFERENCE
    writes = []
    monkeypatch.setattr(repair.profiles, "_s3_put", lambda bucket, key, body: writes.append(key))
    await repair.repair_owner("org", "credentials", apply=True)
    assert len(writes) == 1 and writes[0].endswith(".revision")


@pytest.mark.asyncio
async def test_trial_repair_aborts_on_billing_transition(monkeypatch):
    monkeypatch.setattr(
        repair.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": "runtime", "bedrock_aip_arns": {"sonnet": "arn:sonnet"}}),
    )
    monkeypatch.setattr(
        repair.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            side_effect=[
                {"subscription_status": "trial_local"},
                {"subscription_status": "active"},
                {"subscription_status": "active"},
            ]
        ),
    )
    monkeypatch.setattr(repair, "load_agent_roster", AsyncMock(return_value=[{"id": "agent"}]))
    monkeypatch.setattr(repair, "_profile_repair", lambda *_: pytest.fail("must not patch a paid profile"))
    reconcile = AsyncMock()
    monkeypatch.setattr(repair, "reconcile_hermes_aips", reconcile)
    with pytest.raises(ValueError, match="Billing changed"):
        await repair.repair_owner("org", "trial", apply=True)
    reconcile.assert_not_awaited()
