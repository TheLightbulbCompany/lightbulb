"""`_ensure_network_member`: every prepare writes a missing sessions file and refreshes an
existing one, so a workspace always carries its current `members`."""

from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning, network_member


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "bucket")


@pytest.mark.asyncio
async def test_an_existing_sessions_file_is_republished(monkeypatch):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=True))
    publish, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(network_member, "publish_sessions", publish)
    monkeypatch.setattr(network_member, "write_sessions", write)
    await hermes_provisioning._ensure_network_member("org_x")
    publish.assert_awaited_once_with("org_x")
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_sessions_file_is_written(monkeypatch):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=False))
    publish, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(network_member, "publish_sessions", publish)
    monkeypatch.setattr(network_member, "write_sessions", write)
    await hermes_provisioning._ensure_network_member("org_x")
    write.assert_awaited_once_with("org_x")
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failure_never_raises(monkeypatch):
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(side_effect=RuntimeError("s3")))
    await hermes_provisioning._ensure_network_member("org_x")
