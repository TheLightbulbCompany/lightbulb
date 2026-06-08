"""Tests for per-owner task-role threading + bedrock AIP provision wiring."""

from unittest.mock import MagicMock, patch

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
def mock_ecs_client():
    client = MagicMock()
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/shared-task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "ghcr.io/openclaw:1"}],
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
            "cpu": "512",
            "memory": "2048",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9"
        }
    }
    return client


@pytest.fixture
def manager(mock_settings, mock_ecs_client):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.side_effect = lambda service, **kw: mock_ecs_client if service == "ecs" else MagicMock()
        mgr = EcsManager()
    return mgr


def test_build_kwargs_overrides_task_role(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"


def test_build_kwargs_defaults_to_base_role_when_none(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base("fsap-user")
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/shared-task-role"


def test_register_task_definition_passes_owner_role(manager, mock_ecs_client):
    manager._register_task_definition(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
    assert reg_kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
