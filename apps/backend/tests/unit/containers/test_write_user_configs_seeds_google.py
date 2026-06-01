"""Tests for the per-agent Google-identity seeding wire-up in write_user_configs.

After the friday/atlas pre-stage, write_user_configs loops every Paperclip
agent and seeds its Google account into IDENTITY.md best-effort — a seeder
failure must not break provisioning.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")


@pytest.fixture
def manager():
    from core.containers.ecs_manager import EcsManager

    return EcsManager()


def _make_async_httpx_client_mock() -> MagicMock:
    instance = MagicMock()
    instance.aclose = AsyncMock()
    return MagicMock(return_value=instance)


def _common_patches(manager, *, google_seeder, write_oc=None, ensure_devices=None):
    """Patch every external dependency of write_user_configs except the Google seeder."""
    paperclip_agents = [
        {"id": "uuid-friday", "role": "ceo", "name": "Friday"},
        {"id": "uuid-atlas", "role": "recruiter", "name": "Atlas"},
    ]
    return paperclip_agents, [
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=paperclip_agents),
        ),
        patch("core.containers.ecs_manager.write_openclaw_config", write_oc or AsyncMock()),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch("core.services.google_identity.seed_agent_google_identity", google_seeder),
        patch(
            "core.services.paperclip_api_key.ensure_paperclip_api_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner",
            AsyncMock(return_value=None),
        ),
        patch.object(manager, "ensure_device_identities", ensure_devices or AsyncMock()),
        patch("httpx.AsyncClient", _make_async_httpx_client_mock()),
    ]


@pytest.mark.asyncio
async def test_seeds_google_identity_for_each_agent(monkeypatch, tmp_path, manager):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    google_seeder = AsyncMock()
    agents, patches = _common_patches(manager, google_seeder=google_seeder)

    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await manager.write_user_configs(
            user_id="org_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
        )

    # Called once per Paperclip agent with (owner_id, agent_id).
    assert google_seeder.await_count == len(agents)
    seeded_agent_ids = {c.args[1] for c in google_seeder.await_args_list}
    assert seeded_agent_ids == {a["id"] for a in agents}
    for call in google_seeder.await_args_list:
        assert call.args[0] == "org_abc"


@pytest.mark.asyncio
async def test_google_seeder_failure_does_not_break_provision(monkeypatch, tmp_path, manager):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    google_seeder = AsyncMock(side_effect=RuntimeError("EFS hiccup"))
    ensure_devices = AsyncMock()
    agents, patches = _common_patches(manager, google_seeder=google_seeder, ensure_devices=ensure_devices)

    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        # Must not raise even though every Google seed raises.
        await manager.write_user_configs(
            user_id="org_abc",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            byo_provider=None,
        )

    # Provisioning continued past the seed loop to device identities.
    ensure_devices.assert_awaited_once()
    assert google_seeder.await_count == len(agents)
