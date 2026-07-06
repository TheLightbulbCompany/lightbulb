"""Hermes provisioner facet (H2.3): container_spec / write_config / write_auth.

Three layers, mirroring the OpenClaw provisioner tests + the transport tests'
pure-then-integration split:
  1. ``container_spec`` — assert the ContainerSpec fields against the Hermes
     constants (base task-def family, port 8642, TCP healthcheck, EFS layout).
     What's facet-consumed (healthcheck/efs_mount) vs CDK-owned (image family,
     cpu/memory, base env) is documented in the adapter.
  2. ``_build_hermes_config`` — pure builder, one assertion per provider_choice,
     so a config-brick-inducing drift fails loudly (an invalid config.yaml bricks
     the daemon at boot).
  3. ``write_config`` / ``write_auth`` — integration over a real tmp EFS root:
     write the file, reload it, assert well-formedness + required keys + no
     secrets leaked.
"""

import dataclasses
import json

import pytest
import yaml

from core.config import settings
from core.runtime import get_runtime
from core.runtime.base import (
    ContainerSpec,
    ProviderCreds,
    ProvisionCtx,
)
from core.runtime.hermes.adapter import (
    HERMES_API_PORT,
    _build_hermes_config,
)


# --- container_spec: Hermes launch values -----------------------------------


def test_container_spec_image_is_hermes_base_family():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk", owner_type="org"))
    assert isinstance(spec, ContainerSpec)
    # Mirrors OpenClaw returning isol8-{env}-openclaw — the CDK base task-def
    # family the per-user clone inherits its image/port/base-env from.
    assert spec.image == f"isol8-{settings.ENVIRONMENT or 'dev'}-hermes"


def test_container_spec_port_is_8642():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    assert spec.container_port == HERMES_API_PORT == 8642


def test_container_spec_cpu_memory_match_per_user_size():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    # Same per-user task size as OpenClaw; not consumed by launch (CDK-owned).
    assert spec.cpu == "512"
    assert spec.memory_mib == "2048"


def test_container_spec_healthcheck_is_tcp_on_8642():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    # Facet-consumed: is_healthy TCP-probes this port once H3 threads the owner.
    assert spec.healthcheck.kind == "tcp"
    assert spec.healthcheck.port == HERMES_API_PORT
    assert spec.healthcheck.timeout_s == 5.0


def test_container_spec_env_is_empty():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk-xyz"))
    # API_SERVER_KEY is a per-container SECRET injected by H3, and the static
    # base env lives on the CDK base task-def — so container_spec.env is empty.
    assert spec.env == {}


def test_container_spec_efs_mount_layout():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    assert spec.efs_mount.mount_root == settings.EFS_MOUNT_PATH
    assert spec.efs_mount.owner_subpath == "/users/org_1"
    assert spec.efs_mount.posix_uid == 1000
    assert spec.efs_mount.posix_gid == 1000
    assert spec.efs_mount.root_permissions == "0755"


def test_container_spec_dtos_are_frozen():
    rt = get_runtime("hermes")
    spec = rt.container_spec("org_1", ProvisionCtx(hook_token="hk"))
    assert dataclasses.is_dataclass(spec) and spec.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.container_port = 1  # type: ignore[misc]


# --- _build_hermes_config: minimal provider block per provider_choice -------


def test_build_config_chatgpt_oauth():
    cfg = _build_hermes_config("chatgpt_oauth", None)
    assert cfg["model"]["provider"] == "openai"
    assert cfg["model"]["default"] == "openai/gpt-5.5"


def test_build_config_byo_openai():
    cfg = _build_hermes_config("byo_key", "openai")
    assert cfg["model"]["provider"] == "openai"


def test_build_config_byo_anthropic():
    cfg = _build_hermes_config("byo_key", "anthropic")
    assert cfg["model"]["provider"] == "anthropic"
    assert cfg["model"]["default"].startswith("anthropic/")


def test_build_config_bedrock_has_region_base_url():
    cfg = _build_hermes_config("bedrock_claude", None)
    assert cfg["model"]["provider"] == "bedrock"
    assert cfg["model"]["base_url"].startswith("https://bedrock-runtime.")


def test_build_config_byo_missing_provider_raises():
    with pytest.raises(ValueError, match="byo_provider"):
        _build_hermes_config("byo_key", None)


def test_build_config_unknown_choice_raises():
    with pytest.raises(ValueError, match="Unknown provider_choice"):
        _build_hermes_config("nonsense", None)


# --- write_config: real config.yaml on a tmp EFS root -----------------------


@pytest.mark.asyncio
async def test_write_config_emits_well_formed_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    from core.runtime.base import ConfigProfile

    profile = ConfigProfile(provider_choice="byo_key", byo_provider="anthropic")
    await rt.write_config("org_1", profile)

    config_path = tmp_path / "org_1" / "config.yaml"
    assert config_path.exists()
    # Well-formed YAML with the required provider block — config-brick guard.
    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["model"]["provider"] == "anthropic"
    assert "default" in loaded["model"]


@pytest.mark.asyncio
async def test_write_config_writes_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    from core.runtime.base import ConfigProfile

    profile = ConfigProfile(
        provider_choice="byo_key",
        byo_provider="openai",
        container_auth_token="super-secret-token",
    )
    await rt.write_config("org_1", profile)
    text = (tmp_path / "org_1" / "config.yaml").read_text()
    # No auth material is written into config — it rides env/secret/IAM paths.
    assert "super-secret-token" not in text


@pytest.mark.asyncio
async def test_write_config_unknown_provider_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    from core.runtime.base import ConfigProfile

    profile = ConfigProfile(provider_choice="nonsense")
    with pytest.raises(ValueError, match="Unknown provider_choice"):
        await rt.write_config("org_1", profile)


# --- write_auth: OAuth staged to ~/.hermes/auth.json ------------------------


@pytest.mark.asyncio
async def test_write_auth_oauth_stages_auth_json(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    creds = ProviderCreds(
        kind="oauth",
        secrets={"access_token": "a", "refresh_token": "r", "expires_at": 123},
    )
    await rt.write_auth("org_1", creds)

    auth_path = tmp_path / "org_1" / "auth.json"
    assert auth_path.exists()
    loaded = json.loads(auth_path.read_text())
    assert loaded["tokens"]["access_token"] == "a"
    assert loaded["tokens"]["refresh_token"] == "r"


@pytest.mark.asyncio
async def test_write_auth_non_oauth_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    creds = ProviderCreds(kind="byo_key", secrets={"api_key": "sk-x"})
    with pytest.raises(ValueError, match="oauth"):
        await rt.write_auth("org_1", creds)


@pytest.mark.asyncio
async def test_write_auth_missing_required_keys_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = get_runtime("hermes")
    creds = ProviderCreds(kind="oauth", secrets={"access_token": "a"})
    with pytest.raises(ValueError, match="missing required keys"):
        await rt.write_auth("org_1", creds)
