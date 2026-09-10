"""EcsManager._deployed_image_tag — the image tag fork-gated config keys are
written against (see openclaw_version.deployed_fork_at_least)."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager

_ARN = "arn:aws:ecs:us-east-1:1:task-definition/isol8-prod-openclaw-user:269"


@pytest.fixture
def ecs_client():
    return MagicMock()


@pytest.fixture
def manager(ecs_client):
    with patch("core.containers.ecs_manager.settings") as s, patch("core.containers.ecs_manager.boto3") as b:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        b.client.side_effect = lambda service, **kw: ecs_client if service == "ecs" else MagicMock()
        yield EcsManager()


def _row(monkeypatch, row):
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row))


@pytest.mark.asyncio
async def test_no_container_row_means_fresh_provision(manager, ecs_client, monkeypatch):
    _row(monkeypatch, None)
    assert await manager._deployed_image_tag("org_1") is None
    ecs_client.describe_task_definition.assert_not_called()


@pytest.mark.asyncio
async def test_row_without_task_definition_means_fresh_provision(manager, monkeypatch):
    _row(monkeypatch, {"owner_id": "org_1"})
    assert await manager._deployed_image_tag("org_1") is None


@pytest.mark.asyncio
async def test_returns_the_registered_image_tag(manager, ecs_client, monkeypatch):
    _row(monkeypatch, {"owner_id": "org_1", "task_definition_arn": _ARN})
    ecs_client.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "image": "1.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.7.2-isol8.8-8f700f0",
                }
            ]
        }
    }
    assert await manager._deployed_image_tag("org_1") == "2026.7.2-isol8.8-8f700f0"
    ecs_client.describe_task_definition.assert_called_once_with(taskDefinition=_ARN)


@pytest.mark.asyncio
async def test_unreadable_image_is_unknown_not_pin(manager, ecs_client, monkeypatch):
    _row(monkeypatch, {"owner_id": "org_1", "task_definition_arn": _ARN})
    ecs_client.describe_task_definition.side_effect = RuntimeError("throttled")
    assert await manager._deployed_image_tag("org_1") == ""


@pytest.mark.asyncio
async def test_digest_pinned_image_has_no_tag(manager, ecs_client, monkeypatch):
    _row(monkeypatch, {"owner_id": "org_1", "task_definition_arn": _ARN})
    ecs_client.describe_task_definition.return_value = {
        "taskDefinition": {"containerDefinitions": [{"image": "1.dkr.ecr.us-east-1.amazonaws.com/isol8/x@sha256:abc"}]}
    }
    assert await manager._deployed_image_tag("org_1") == ""


@pytest.mark.asyncio
async def test_base_image_tag_reads_the_family_the_replacement_is_cloned_from(manager, ecs_client):
    ecs_client.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {"image": "1.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.7.2-isol8.6-303527a"}
            ]
        }
    }
    assert await manager._base_image_tag() == "2026.7.2-isol8.6-303527a"
    ecs_client.describe_task_definition.assert_called_once_with(taskDefinition=manager._base_task_def_family)


@pytest.mark.asyncio
async def test_base_image_tag_unreadable_gates_closed(manager, ecs_client):
    ecs_client.describe_task_definition.side_effect = RuntimeError("throttled")
    assert await manager._base_image_tag() == ""


def _write_user_configs_patches(manager, write_oc_mock):
    return (
        patch("core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner", AsyncMock(return_value=[])),
        patch("core.containers.ecs_manager.write_openclaw_config", write_oc_mock),
        patch("core.services.friday_identity.pre_stage_friday_identity", AsyncMock()),
        patch("core.services.atlas_seed.pre_stage_atlas", AsyncMock()),
        patch("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value=None)),
        patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", AsyncMock(return_value=None)),
        patch("core.containers.ecs_manager._provision_mcp_servers", AsyncMock(return_value={})),
        patch("core.containers.ecs_manager._provision_channel_accounts", AsyncMock(return_value=({}, []))),
        patch("core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})),
        patch("core.containers.ecs_manager.get_workspace", MagicMock()),
        patch.object(manager, "ensure_device_identities", AsyncMock()),
        patch("httpx.AsyncClient", MagicMock(return_value=MagicMock(aclose=AsyncMock()))),
    )


@pytest.mark.asyncio
async def test_write_user_configs_prefers_an_explicit_target_image(manager, monkeypatch, tmp_path):
    """Provisioning passes the base image; the owner's old task-def must NOT be consulted."""
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    write_oc = AsyncMock()
    lookup = AsyncMock(return_value="2026.7.2-isol8.8-8f700f0")
    with patch.object(manager, "_deployed_image_tag", lookup):
        with ExitStack() as stack:
            for p in _write_user_configs_patches(manager, write_oc):
                stack.enter_context(p)
            await manager.write_user_configs(
                "org_1", "t" * 32, provider_choice="chatgpt_oauth", deployed_tag="2026.7.2-isol8.6-303527a"
            )
    assert write_oc.call_args.kwargs["deployed_tag"] == "2026.7.2-isol8.6-303527a"
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_user_configs_falls_back_to_the_owners_running_image(manager, monkeypatch, tmp_path):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    write_oc = AsyncMock()
    with patch.object(manager, "_deployed_image_tag", AsyncMock(return_value="2026.7.2-isol8.8-8f700f0")):
        with ExitStack() as stack:
            for p in _write_user_configs_patches(manager, write_oc):
                stack.enter_context(p)
            await manager.write_user_configs("org_1", "t" * 32, provider_choice="chatgpt_oauth")
    assert write_oc.call_args.kwargs["deployed_tag"] == "2026.7.2-isol8.8-8f700f0"
