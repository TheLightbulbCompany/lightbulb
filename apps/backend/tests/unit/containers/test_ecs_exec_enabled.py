"""ECS Exec must be enabled on per-user OpenClaw services.

This is the runtime prerequisite for the AWS FIS ``aws:ecs:task-cpu-stress``
chaos action: that fault injects via the ECS/SSM agent, which only attaches
when the service was created with ``enableExecuteCommand=True`` AND the
per-user task role grants the four ``ssmmessages:*`` actions (granted in CDK
``container-stack.ts`` on ``this.taskRole``). This test pins the
service-creation half of that contract so a future refactor of the
``create_service`` kwargs can't silently drop ECS Exec and break the
game-day fault injection.

Uses mocked boto3 clients + container_repo -- no real AWS or DynamoDB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.fixture(autouse=True)
def _mock_bedrock_mint(monkeypatch):
    """Stub the synchronous per-owner AIP + scoped-role mint that the
    bedrock_claude provision branch runs, so this test never touches the
    Bedrock/IAM control plane. Mirrors test_ecs_manager.py's fixture."""
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_aips",
        AsyncMock(
            return_value={
                "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
                "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
                "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
            }
        ),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_test_123"),
    )


@pytest.fixture
def mock_ecs_client():
    """Mock ECS boto3 client with the responses create_user_service needs."""
    client = MagicMock()
    client.create_service.return_value = {"service": {"serviceName": "openclaw-user_test_123-f4ae64abb2db"}}
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "alpine/openclaw:latest"}],
            "volumes": [
                {
                    "name": "openclaw-workspace",
                    "efsVolumeConfiguration": {
                        "fileSystemId": "fs-test123",
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {"accessPointId": "fsap-base", "iam": "ENABLED"},
                    },
                }
            ],
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw:42"}
    }
    client.deregister_task_definition.return_value = {}
    return client


@pytest.fixture
def mock_efs_client():
    """Mock EFS boto3 client."""
    client = MagicMock()
    client.create_access_point.return_value = {"AccessPointId": "fsap-user123"}
    client.delete_access_point.return_value = {}
    client.exceptions = MagicMock()
    client.exceptions.AccessPointNotFound = type("AccessPointNotFound", (Exception,), {})
    return client


@pytest.fixture
def mock_settings():
    """Mock settings with test ECS configuration."""
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "lightbulb-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings, mock_ecs_client, mock_efs_client):
    """EcsManager with mocked boto3 clients."""
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:

        def client_factory(service, **kwargs):
            if service == "ecs":
                return mock_ecs_client
            if service == "efs":
                return mock_efs_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        return EcsManager()


@pytest.mark.asyncio
async def test_per_user_create_service_enables_execute_command(manager, mock_ecs_client):
    """The per-user create_service call MUST pass enableExecuteCommand=True.

    Without it the SSM agent never attaches to the task, so neither
    ``aws ecs execute-command`` debugging nor the FIS task-cpu-stress action
    can reach the container.
    """
    with patch("core.containers.ecs_manager.container_repo") as mock_repo:
        mock_repo.upsert = AsyncMock(return_value={"owner_id": "user_test_123", "status": "provisioning"})
        mock_repo.update_fields = AsyncMock(return_value={"owner_id": "user_test_123", "status": "provisioning"})

        await manager.create_user_service("user_test_123", "token-abc")

    mock_ecs_client.create_service.assert_called_once()
    call_kwargs = mock_ecs_client.create_service.call_args.kwargs
    assert call_kwargs.get("enableExecuteCommand") is True, (
        "Per-user ECS service must be created with enableExecuteCommand=True so the "
        "SSM agent attaches (ECS Exec debugging + FIS task-cpu-stress prerequisite)."
    )
