"""EcsManager.kill_owner_bedrock fires BOTH halves of the kill-switch:
IAM revoke (Plan 2) + ECS desired=0 (reusing stop_user_service)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.fixture
def mock_settings():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        mgr = EcsManager()
    return mgr


@pytest.mark.asyncio
async def test_kill_fires_iam_revoke_and_ecs_stop(manager):
    revoke_mock = AsyncMock()
    with (
        patch(
            "core.billing.bedrock_aip.revoke_owner_bedrock",
            new=revoke_mock,
        ),
        patch.object(manager, "stop_user_service", new=AsyncMock()) as stop_mock,
    ):
        await manager.kill_owner_bedrock("org_Y")
    revoke_mock.assert_awaited_once_with("org_Y")
    stop_mock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_ecs_stop_runs_even_if_iam_revoke_raises(manager):
    """IAM revoke is eventually-consistent and may flake; the ECS desired=0
    cut MUST still fire — it is the guaranteed instant stop."""
    revoke_mock = AsyncMock(side_effect=RuntimeError("iam throttled"))
    with (
        patch(
            "core.billing.bedrock_aip.revoke_owner_bedrock",
            new=revoke_mock,
        ),
        patch.object(manager, "stop_user_service", new=AsyncMock()) as stop_mock,
    ):
        await manager.kill_owner_bedrock("org_Y")
    revoke_mock.assert_awaited_once_with("org_Y")
    stop_mock.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_is_owner_service_stopped_true_when_status_stopped(manager):
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "stopped"}),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is True


@pytest.mark.asyncio
async def test_is_owner_service_stopped_false_when_running(manager):
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={"status": "running"}),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is False


@pytest.mark.asyncio
async def test_is_owner_service_stopped_true_when_no_row(manager):
    """No container row → nothing to kill → treat as already stopped."""
    with patch(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        assert await manager.is_owner_service_stopped("org_Y") is True
