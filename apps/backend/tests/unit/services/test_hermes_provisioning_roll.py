"""The pool is CDK-owned. Per-owner rolls replace only idle sessions on that pool."""

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.services import container_rpc, hermes_provisioning

IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-new"
POOL = "arn:rt"
ROW = {"owner_id": "org_a", "runtime_host": "agentcore", "agentcore_shared_runtime_arn": POOL, "status": "running"}


def _wire(monkeypatch, *, current_image, row=ROW):
    """Actual pool deployment timestamp and session generation, with AWS I/O stubbed."""
    now = time.time()
    monkeypatch.setattr(hermes_provisioning.settings, "HERMES_SHARED_RUNTIME_ARN", POOL)
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_session_sync",
        lambda *a, **k: {
            "arn": POOL,
            "warm_until": int(now) + 500,
            "started_at": int(now) - (50 if current_image == IMAGE else 200),
        },
    )
    monkeypatch.setattr(
        hermes_provisioning,
        "_runtime_description",
        lambda arn: {
            "lastUpdatedAt": datetime.fromtimestamp(now - 100, timezone.utc),
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
        },
    )
    monkeypatch.setattr(
        hermes_provisioning, "_update_runtime_image", lambda *a: pytest.fail("per-owner roll changed the CDK pool")
    )
    calls = []
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda arn, sid: calls.append(("stop", arn, sid)))

    async def health(owner_id, method, params=None, **kwargs):
        calls.append(("health", owner_id))
        return {"busy": 0}

    monkeypatch.setattr(container_rpc, "call", health)
    monkeypatch.setattr(hermes_provisioning, "_session_warm", AsyncMock(return_value=False))
    return calls


@pytest.mark.asyncio
async def test_current_pool_session_is_not_stopped_or_respecified(monkeypatch):
    calls = _wire(monkeypatch, current_image=IMAGE)
    assert await hermes_provisioning.roll_runtime_image("org_a") == "current"
    assert calls == []


@pytest.mark.asyncio
async def test_pool_roll_dry_run_preserves_session_and_cdk_spec(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old")
    assert await hermes_provisioning.roll_runtime_image("org_a", dry_run=True) == "would-update"
    assert calls == []


@pytest.mark.asyncio
async def test_roll_refuses_unprovisioned_workspace(monkeypatch):
    calls = _wire(monkeypatch, current_image=IMAGE, row={"owner_id": "org_a", "status": "provisioning"})
    with pytest.raises(ValueError, match="no provisioned runtime"):
        await hermes_provisioning.roll_runtime_image("org_a")
    assert calls == []
