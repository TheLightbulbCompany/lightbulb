"""Tests for the friday-identity seeding wire-up in ecs_manager.write_user_configs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


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
    atlas_seeder_mock = AsyncMock()
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
        patch(
            "core.services.atlas_seed.pre_stage_atlas",
            atlas_seeder_mock,
        ),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value=None),
        ),
        patch.object(manager, "ensure_device_identities", ensure_devices_mock),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
            aip_arns=_AIPS,
        )

    write_oc_mock.assert_awaited_once()
    seeder_mock.assert_awaited_once()
    seeder_kwargs = seeder_mock.call_args.kwargs
    assert seeder_kwargs["owner_id"] == "user_abc"
    assert seeder_kwargs["paperclip_agents"] == paperclip_agents

    atlas_seeder_mock.assert_awaited_once()
    atlas_kwargs = atlas_seeder_mock.call_args.kwargs
    assert atlas_kwargs["owner_id"] == "user_abc"
    assert atlas_kwargs["paperclip_agents"] == paperclip_agents


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
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value=None),
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
            aip_arns=_AIPS,
        )

    seeder_mock.assert_awaited_once()
    ensure_devices_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_mints_a_key_for_every_agent_and_stages_per_agent_identities(monkeypatch, tmp_path, manager):
    """write_user_configs mints a Paperclip key for EVERY agent (not just the
    CEO) and threads the {agent_id: key} map + company_id into the per-agent
    identity-file seeder."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://pc.internal:3100")

    paperclip_agents = [
        {"id": "agent-friday", "role": "ceo", "name": "Friday"},
        {"id": "agent-atlas", "role": "general", "name": "Atlas"},
    ]

    minted: dict[str, str] = {}

    async def fake_ensure_key(*, owner_id, paperclip_agent_id, admin_client):
        token = f"key-{paperclip_agent_id}"
        minted[paperclip_agent_id] = token
        return token

    ensure_key_mock = AsyncMock(side_effect=fake_ensure_key)
    identities_mock = AsyncMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=paperclip_agents),
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", AsyncMock()),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.friday_identity.pre_stage_paperclip_identities", identities_mock),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch("core.services.paperclip_api_key.ensure_paperclip_api_key", ensure_key_mock),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value="comp-1"),
        ),
        patch.object(manager, "ensure_device_identities", AsyncMock()),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
            aip_arns=_AIPS,
        )

    # A key was minted for BOTH agents, not just the CEO.
    assert ensure_key_mock.await_count == 2
    assert minted == {"agent-friday": "key-agent-friday", "agent-atlas": "key-agent-atlas"}

    # The per-agent identity seeder received the full key map + company_id + url.
    identities_mock.assert_awaited_once()
    kw = identities_mock.call_args.kwargs
    assert kw["owner_id"] == "user_abc"
    assert kw["agent_keys"] == {"agent-friday": "key-agent-friday", "agent-atlas": "key-agent-atlas"}
    assert kw["company_id"] == "comp-1"
    assert kw["api_url"] == "http://pc.internal:3100"
    assert kw["paperclip_agents"] == paperclip_agents


@pytest.mark.asyncio
async def test_one_agent_key_failure_does_not_block_the_rest(monkeypatch, tmp_path, manager):
    """A single agent's mint returning None is logged + skipped; the others
    still get keys and the map omits the failed agent."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://pc.internal:3100")

    paperclip_agents = [
        {"id": "agent-ok", "role": "ceo"},
        {"id": "agent-fail", "role": "general"},
    ]

    async def fake_ensure_key(*, owner_id, paperclip_agent_id, admin_client):
        return None if paperclip_agent_id == "agent-fail" else "key-ok"

    identities_mock = AsyncMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=paperclip_agents),
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", AsyncMock()),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.friday_identity.pre_stage_paperclip_identities", identities_mock),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(side_effect=fake_ensure_key),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value="comp-1"),
        ),
        patch.object(manager, "ensure_device_identities", AsyncMock()),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
            aip_arns=_AIPS,
        )

    kw = identities_mock.call_args.kwargs
    assert kw["agent_keys"] == {"agent-ok": "key-ok"}  # failed agent omitted


@pytest.mark.asyncio
async def test_passes_channel_accounts_to_write_openclaw_config(monkeypatch, tmp_path, manager):
    """write_user_configs carries channel accounts across the full overwrite by
    forwarding _provision_channel_accounts() result to write_openclaw_config."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    carried = {"telegram": {"agent_a": {"botToken": "123:ABC"}}}
    write_oc_mock = AsyncMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=[{"id": "uuid-friday", "role": "ceo"}]),
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", write_oc_mock),
        patch(
            "core.containers.ecs_manager._provision_channel_accounts",
            AsyncMock(return_value=carried),
        ),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value=None),
        ),
        patch.object(manager, "ensure_device_identities", AsyncMock()),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
            aip_arns=_AIPS,
        )

    write_oc_mock.assert_awaited_once()
    assert write_oc_mock.call_args.kwargs["channel_accounts"] == carried


@pytest.mark.asyncio
async def test_writes_owner_facts(monkeypatch, tmp_path, manager):
    """write_user_configs refreshes the owner-facts file for the owner."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    facts_mock = AsyncMock()

    with (
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=[{"id": "uuid-friday", "role": "ceo"}]),
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", AsyncMock()),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value=None),
        ),
        patch("core.services.owner_facts.write_owner_facts", facts_mock),
        patch.object(manager, "ensure_device_identities", AsyncMock()),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ):
        await manager.write_user_configs(
            user_id="user_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
            aip_arns=_AIPS,
        )

    facts_mock.assert_awaited_once_with("user_abc")
