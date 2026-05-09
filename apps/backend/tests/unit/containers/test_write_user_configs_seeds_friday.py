"""Tests for the friday-identity seeding wire-up in ecs_manager.write_user_configs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    """Force ENVIRONMENT=local so os.chown is skipped on macOS."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")


@pytest.fixture
def manager(monkeypatch):
    """Construct an EcsManager without touching boto3."""
    from core.containers.ecs_manager import EcsManager

    # boto3 clients aren't needed for write_user_configs; patch the EFS root
    # to a tmpdir-sized stub via settings.EFS_MOUNT_PATH at the call site.
    return EcsManager()


def _make_async_httpx_client_mock() -> MagicMock:
    """Build an httpx.AsyncClient stand-in whose `.aclose()` is awaitable.

    `write_user_configs` calls `await http.aclose()` in its `finally` block,
    so a plain `MagicMock` (default `patch` substitute) raises TypeError on
    the await. We return a sync MagicMock with `.aclose` patched to an
    AsyncMock so the call sequence type-checks.
    """
    instance = MagicMock()
    instance.aclose = AsyncMock()
    factory = MagicMock(return_value=instance)
    return factory


@pytest.mark.asyncio
async def test_invokes_seeder_after_write_openclaw_config(monkeypatch, tmp_path, manager):
    """write_user_configs calls pre_stage_friday_identity AFTER write_openclaw_config."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    paperclip_agents = [{"id": "uuid-friday", "role": "ceo", "name": "Friday"}]

    fetch_mock = AsyncMock(return_value=paperclip_agents)
    write_oc_mock = AsyncMock()
    seeder_mock = AsyncMock()
    ensure_devices_mock = AsyncMock()

    # Patch every external dependency of write_user_configs.
    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            fetch_mock,
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", write_oc_mock),
        patch(
            "core.services.friday_identity.pre_stage_friday_identity",
            seeder_mock,
        ),
        patch.object(manager, "ensure_device_identities", ensure_devices_mock),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
        )

    write_oc_mock.assert_awaited_once()
    seeder_mock.assert_awaited_once()
    seeder_kwargs = seeder_mock.call_args.kwargs
    assert seeder_kwargs["owner_id"] == "user_abc"
    assert seeder_kwargs["paperclip_agents"] == paperclip_agents


@pytest.mark.asyncio
async def test_swallows_seeder_failure_and_continues(monkeypatch, tmp_path, manager):
    """Seeder raises -> write_user_configs still calls ensure_device_identities."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    fetch_mock = AsyncMock(return_value=[{"id": "uuid-1", "role": "ceo"}])
    write_oc_mock = AsyncMock()
    seeder_mock = AsyncMock(side_effect=RuntimeError("EFS hiccup"))
    ensure_devices_mock = AsyncMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            fetch_mock,
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", write_oc_mock),
        patch(
            "core.services.friday_identity.pre_stage_friday_identity",
            seeder_mock,
        ),
        patch.object(manager, "ensure_device_identities", ensure_devices_mock),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        # Should not raise even though the seeder raised.
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
        )

    seeder_mock.assert_awaited_once()
    ensure_devices_mock.assert_awaited_once()
