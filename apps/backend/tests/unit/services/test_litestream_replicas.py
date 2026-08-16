"""delete_replica_prefix — the O6 S3 replica-lifecycle helper.

The container boot script RESTORES whatever survives under the owner's S3
prefix, so wipe/reprovision and agent-delete flows depend on this helper
actually clearing it; conversely it must be a pure no-op on EFS-layout envs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import litestream_replicas


@pytest.fixture(autouse=True)
def no_harvest(monkeypatch):
    """delete_replica_prefix runs the OAuth seed harvest before a full-owner
    delete (it late-imports from oauth_service, so patch the source module).
    Keep it inert here — these tests cover the prefix lifecycle; the harvest
    has its own suite (test_oauth_seed_harvest.py)."""
    from core.services import oauth_service

    mock = AsyncMock(return_value=False)
    monkeypatch.setattr(oauth_service, "harvest_rotated_oauth_seed", mock)
    return mock


@pytest.mark.asyncio
async def test_noop_when_bucket_unset(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "", raising=False)
    with patch("boto3.client") as client:
        assert await litestream_replicas.delete_replica_prefix("org_1") is True
        client.assert_not_called()


@pytest.mark.asyncio
async def test_deletes_owner_prefix_in_batches(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    pages = [
        {"Contents": [{"Key": f"org_1/agents/a/agent/openclaw-agent.sqlite/0000/{i}.ltx"} for i in range(2)]},
        {"Contents": [{"Key": "org_1/state/openclaw.sqlite/0000/1.ltx"}]},
        {},  # empty trailing page must not call delete_objects
    ]
    s3.get_paginator.return_value.paginate.return_value = iter(pages)
    s3.delete_objects.return_value = {}  # no per-key Errors
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1") is True
    assert s3.get_paginator.return_value.paginate.call_args.kwargs == {"Bucket": "b", "Prefix": "org_1/"}
    assert s3.delete_objects.call_count == 2


@pytest.mark.asyncio
async def test_agent_scoped_prefix(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter([{}])
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1", agent_id="agent-x") is True
    assert s3.get_paginator.return_value.paginate.call_args.kwargs["Prefix"] == "org_1/agents/agent-x/"


@pytest.mark.asyncio
async def test_unsafe_agent_id_refused(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    with patch("boto3.client") as client:
        assert await litestream_replicas.delete_replica_prefix("org_1", agent_id="../evil") is False
        client.assert_not_called()


@pytest.mark.asyncio
async def test_failure_returns_false_never_raises(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = RuntimeError("s3 down")
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1") is False


@pytest.mark.asyncio
async def test_per_object_delete_errors_are_failures(monkeypatch):
    """DeleteObjects is partial-success: per-key failures land in Errors without
    raising. Reporting success there would leave restorable OAuth-bearing rows
    behind while the caller believes the wipe was clean."""
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter(
        [{"Contents": [{"Key": "org_1/state/openclaw.sqlite/0000/1.ltx"}]}]
    )
    s3.delete_objects.return_value = {
        "Errors": [{"Key": "org_1/state/openclaw.sqlite/0000/1.ltx", "Code": "AccessDenied"}]
    }
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1") is False


@pytest.mark.asyncio
async def test_full_owner_delete_harvests_oauth_seed_first(monkeypatch, no_harvest):
    """The full-owner prefix delete destroys the ONLY backend-reachable copy of
    the rotated ChatGPT refresh token (the default agent's DB replica), so the
    harvest must run before it — or the next provision stages a spent seed."""
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter([{}])
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1") is True
    no_harvest.assert_awaited_once_with(owner_id="org_1")


@pytest.mark.asyncio
async def test_agent_scoped_delete_does_not_harvest(monkeypatch, no_harvest):
    """Deleting one agent's prefix leaves the default agent's store alive —
    nothing is being lost, so no harvest."""
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter([{}])
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1", agent_id="agent-x") is True
    no_harvest.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_failure_never_blocks_the_delete(monkeypatch, no_harvest):
    """Best-effort: the wipe's primary operation already succeeded, so a broken
    harvest (S3 hiccup, corrupt replica) must not abort the cleanup."""
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    no_harvest.side_effect = RuntimeError("restore blew up")
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = iter([{}])
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.delete_replica_prefix("org_1") is True


@pytest.mark.asyncio
async def test_cleanup_deleted_agent_reclaims_efs_and_replicas(monkeypatch):
    """Single chokepoint for every agents.delete path: EFS dirs AND the S3
    replica prefix. Missing the replica half re-materializes the deleted
    agent's DB on every subsequent boot (restore enumerates S3, not config)."""
    from core.containers import workspace as ws_mod

    ws = MagicMock()
    monkeypatch.setattr(ws_mod, "get_workspace", lambda: ws)
    with patch("core.services.litestream_replicas.delete_replica_prefix", new=AsyncMock(return_value=True)) as drp:
        with patch(
            "core.services.litestream_replicas.write_replica_tombstone", new=AsyncMock(return_value=True)
        ) as tomb:
            await ws_mod.cleanup_deleted_agent("org_1", "agent-x")
    tomb.assert_awaited_once_with("org_1", "agent-x")
    ws.cleanup_agent_dirs.assert_called_once_with("org_1", "agent-x")
    drp.assert_awaited_once_with("org_1", agent_id="agent-x")


@pytest.mark.asyncio
async def test_cleanup_deleted_agent_still_drops_replicas_when_efs_fails(monkeypatch):
    from core.containers import workspace as ws_mod

    ws = MagicMock()
    ws.cleanup_agent_dirs.side_effect = RuntimeError("efs down")
    monkeypatch.setattr(ws_mod, "get_workspace", lambda: ws)
    with patch("core.services.litestream_replicas.delete_replica_prefix", new=AsyncMock(return_value=True)) as drp:
        with patch("core.services.litestream_replicas.write_replica_tombstone", new=AsyncMock(return_value=True)):
            await ws_mod.cleanup_deleted_agent("org_1", "agent-x")  # must not raise
    drp.assert_awaited_once()


@pytest.mark.asyncio
async def test_tombstone_written_under_the_agent_prefix(monkeypatch):
    """The tombstone — not a timed second delete — is what makes agent deletion
    correct: an in-flight/throttled Litestream upload can land after ANY delay,
    so the next boot must be told to purge and not restore."""
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    s3 = MagicMock()
    with patch("boto3.client", return_value=s3):
        assert await litestream_replicas.write_replica_tombstone("org_1", "agent-x") is True
    s3.put_object.assert_called_once_with(Bucket="b", Key="org_1/agents/agent-x/.replica-deleted", Body=b"")


@pytest.mark.asyncio
async def test_tombstone_noop_without_bucket_and_rejects_unsafe_id(monkeypatch):
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "", raising=False)
    with patch("boto3.client") as c:
        assert await litestream_replicas.write_replica_tombstone("org_1", "a") is True
        c.assert_not_called()
    monkeypatch.setattr(litestream_replicas.settings, "LITESTREAM_BUCKET", "b", raising=False)
    with patch("boto3.client") as c:
        assert await litestream_replicas.write_replica_tombstone("org_1", "../evil") is False
        c.assert_not_called()
