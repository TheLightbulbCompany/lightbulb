"""Provisioner facet of the OpenClaw runtime adapter.

``container_spec`` must reflect the SAME launch values the existing ECS
provisioning path uses (this facet is a MOVE, not a change) — so the tests
assert the spec fields against the real ``ecs_manager`` constants/literals.
``write_config`` / ``write_auth`` are thin delegations, verified by patching
the real target functions and asserting they were awaited with the mapped
params.
"""

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.runtime import get_runtime
from core.runtime.base import (
    ConfigProfile,
    ContainerSpec,
    ProviderCreds,
    ProvisionCtx,
)


# --- container_spec: same values as ecs_manager -----------------------------


def test_container_spec_matches_ecs_manager_launch_values():
    # Assert against the REAL provisioning values, imported from ecs_manager,
    # so the spec can never silently drift from the path it mirrors.
    from core.containers.ecs_manager import (
        GATEWAY_PORT,
        PER_USER_CPU,
        PER_USER_MEMORY_MIB,
        EcsManager,
    )

    rt = get_runtime("openclaw")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk-abc", owner_type="org"))

    assert isinstance(spec, ContainerSpec)
    # Image source: the CDK base task-def family the per-user clone inherits
    # its image from (ecs_manager._build_register_kwargs_from_base clones it).
    assert spec.image == EcsManager._BASE_TASK_DEF_FAMILY.format(env=settings.ENVIRONMENT or "dev")
    assert spec.container_port == GATEWAY_PORT == 18789
    assert spec.cpu == PER_USER_CPU == "512"
    assert spec.memory_mib == PER_USER_MEMORY_MIB == "2048"


def test_container_spec_env_carries_hook_token():
    # ecs_manager._build_per_user_environment injects exactly OPENCLAW_HOOK_TOKEN.
    rt = get_runtime("openclaw")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk-xyz"))
    assert spec.env == {"OPENCLAW_HOOK_TOKEN": "hk-xyz"}


def test_container_spec_healthcheck_is_tcp_on_gateway_port():
    from core.containers.ecs_manager import GATEWAY_PORT

    rt = get_runtime("openclaw")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    # is_healthy() does socket.create_connection((ip, GATEWAY_PORT), timeout=5).
    assert spec.healthcheck.kind == "tcp"
    assert spec.healthcheck.port == GATEWAY_PORT
    assert spec.healthcheck.timeout_s == 5.0


def test_container_spec_efs_mount_layout():
    # ecs_manager._create_access_point roots at /users/{owner_id}, PosixUser
    # 1000/1000, root perms 0755; backend mount root is settings.EFS_MOUNT_PATH.
    rt = get_runtime("openclaw")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    assert spec.efs_mount.mount_root == settings.EFS_MOUNT_PATH
    assert spec.efs_mount.owner_subpath == "/users/org_1"
    assert spec.efs_mount.posix_uid == 1000
    assert spec.efs_mount.posix_gid == 1000
    assert spec.efs_mount.root_permissions == "0755"


def test_container_spec_dtos_are_frozen():
    rt = get_runtime("openclaw")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    assert dataclasses.is_dataclass(spec) and spec.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.container_port = 1  # type: ignore[misc]


# --- write_config: delegates to write_openclaw_config -----------------------


@pytest.mark.asyncio
async def test_write_config_delegates_to_write_openclaw_config():
    rt = get_runtime("openclaw")
    profile = ConfigProfile(
        provider_choice="byo_key",
        container_auth_token="gw-tok",
        byo_provider="openai",
        hook_token="hk-tok",
        owner_type="org",
        runtime_config={"paperclip_agents": [{"id": "a1"}], "aip_arns": {"sonnet": "arn:x"}},
    )
    with patch(
        "core.containers.config.write_openclaw_config",
        new=AsyncMock(),
    ) as woc:
        await rt.write_config("org_1", profile)

    woc.assert_awaited_once()
    kwargs = woc.call_args.kwargs
    assert kwargs["config_path"] == Path(settings.EFS_MOUNT_PATH) / "org_1" / "openclaw.json"
    assert kwargs["gateway_token"] == "gw-tok"
    assert kwargs["provider_choice"] == "byo_key"
    assert kwargs["user_id"] == "org_1"
    assert kwargs["byo_provider"] == "openai"
    assert kwargs["hook_token"] == "hk-tok"
    assert kwargs["owner_type"] == "org"
    # runtime_config (OpenClaw-specific gathered inputs) forwarded verbatim.
    assert kwargs["paperclip_agents"] == [{"id": "a1"}]
    assert kwargs["aip_arns"] == {"sonnet": "arn:x"}


@pytest.mark.asyncio
async def test_write_config_minimal_profile_forwards_defaults():
    rt = get_runtime("openclaw")
    profile = ConfigProfile(provider_choice="bedrock_claude", container_auth_token="gw")
    with patch(
        "core.containers.config.write_openclaw_config",
        new=AsyncMock(),
    ) as woc:
        await rt.write_config("org_2", profile)
    kwargs = woc.call_args.kwargs
    assert kwargs["provider_choice"] == "bedrock_claude"
    assert kwargs["byo_provider"] is None
    assert kwargs["owner_type"] == "personal"


# --- write_auth: delegates to the EFS OAuth pre-stage -----------------------


@pytest.mark.asyncio
async def test_write_auth_oauth_delegates_to_pre_stage():
    rt = get_runtime("openclaw")
    creds = ProviderCreds(
        kind="oauth",
        secrets={"access_token": "a", "refresh_token": "r", "expires_at": 123},
    )
    with patch(
        "core.containers.workspace.pre_stage_auth_profile_store",
        new=AsyncMock(),
    ) as pre_stage:
        await rt.write_auth("org_1", creds)

    pre_stage.assert_awaited_once()
    kwargs = pre_stage.call_args.kwargs
    assert kwargs["user_id"] == "org_1"
    assert kwargs["oauth_tokens"] == {"access_token": "a", "refresh_token": "r", "expires_at": 123}


@pytest.mark.asyncio
async def test_write_auth_unknown_kind_raises():
    rt = get_runtime("openclaw")
    creds = ProviderCreds(kind="byo_key", secrets={"api_key": "sk-x"})
    with pytest.raises(ValueError, match="oauth"):
        await rt.write_auth("org_1", creds)
