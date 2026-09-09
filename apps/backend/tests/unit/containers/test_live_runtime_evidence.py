"""Fail-closed proof of the running OpenClaw task's pinned identity."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager
from core.openclaw_version import PinnedReleaseIdentity

_OWNER_ID = "org_1"
_SERVICE_NAME = "openclaw-org_1-abc123"
_TASK_ARN = "arn:aws:ecs:us-east-1:1:task/cluster/task-1"
_TASK_DEFINITION_ARN = "arn:aws:ecs:us-east-1:1:task-definition/lightbulb-prod-openclaw-user:42"
_IMAGE = "1.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.9.3-isol8.2-449894a"
_RUNTIME_DIGEST = "sha256:" + "6" * 64

_PIN = PinnedReleaseIdentity(
    fork_tag="v2026.9.3-isol8.2",
    fork_commit="a681accbfd1871845e684937ba7af57ecf155f55",
    base_image_digest="sha256:" + "b" * 64,
    gateway_build_id="a681acc",
    extended_tag="2026.9.3-isol8.2-449894a",
    extended_artifact_digest="sha256:" + "e" * 64,
    runtime_manifest_digest=_RUNTIME_DIGEST,
)


@pytest.fixture
def ecs_client():
    return MagicMock()


@pytest.fixture
def manager(ecs_client):
    with patch("core.containers.ecs_manager.settings") as settings, patch(
        "core.containers.ecs_manager.boto3"
    ) as boto3_mock:
        settings.AWS_REGION = "us-east-1"
        settings.ENVIRONMENT = "prod"
        boto3_mock.client.side_effect = lambda service, **kwargs: ecs_client if service == "ecs" else MagicMock()
        yield EcsManager()


def _row() -> dict:
    return {
        "owner_id": _OWNER_ID,
        "status": "running",
        "service_name": _SERVICE_NAME,
        "task_arn": _TASK_ARN,
        "task_definition_arn": _TASK_DEFINITION_ARN,
    }


def _task_definition() -> dict:
    return {
        "taskDefinition": {
            "taskDefinitionArn": _TASK_DEFINITION_ARN,
            "containerDefinitions": [{"name": "openclaw", "image": _IMAGE}],
        }
    }


def _task() -> dict:
    return {
        "taskArn": _TASK_ARN,
        "taskDefinitionArn": _TASK_DEFINITION_ARN,
        "lastStatus": "RUNNING",
        "desiredStatus": "RUNNING",
        "group": f"service:{_SERVICE_NAME}",
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "privateIPv4Address", "value": "10.0.1.23"}],
            }
        ],
        "containers": [
            {
                "name": "openclaw",
                "image": _IMAGE,
                "imageDigest": _RUNTIME_DIGEST,
                "lastStatus": "RUNNING",
            }
        ],
    }


def _arrange(ecs_client, monkeypatch, *, row=None, task_definition=None, task=None, pin=_PIN):
    get_container = AsyncMock(return_value=_row() if row is None else row)
    update_container = AsyncMock()
    monkeypatch.setattr("core.containers.ecs_manager.container_repo.get_by_owner_id", get_container)
    monkeypatch.setattr("core.containers.ecs_manager.container_repo.update_fields", update_container)
    monkeypatch.setattr("core.containers.ecs_manager.openclaw_version.pinned_release_identity", lambda: pin)
    ecs_client.describe_task_definition.return_value = (
        _task_definition() if task_definition is None else task_definition
    )
    ecs_client.describe_tasks.return_value = {"failures": [], "tasks": [_task() if task is None else task]}
    return get_container, update_container


@pytest.mark.asyncio
async def test_live_runtime_evidence_binds_one_running_task_to_the_pinned_release(
    manager, ecs_client, monkeypatch
):
    get_container, update_container = _arrange(ecs_client, monkeypatch)

    evidence = await manager.live_runtime_evidence(_OWNER_ID)

    assert evidence is not None
    assert evidence.task_arn == _TASK_ARN
    assert evidence.task_definition_arn == _TASK_DEFINITION_ARN
    assert evidence.private_ip == "10.0.1.23"
    assert evidence.gateway_build_id == "a681acc"
    get_container.assert_awaited_once_with(_OWNER_ID, consistent=True)
    ecs_client.describe_tasks.assert_called_once_with(cluster=manager._cluster, tasks=[_TASK_ARN])
    ecs_client.describe_task_definition.assert_called_once_with(taskDefinition=_TASK_DEFINITION_ARN)
    update_container.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "row_not_running",
        "task_not_running",
        "task_stopping",
        "task_arn",
        "task_definition",
        "described_task_definition",
        "service",
        "tag",
        "container_image",
        "runtime_digest",
        "private_ip",
    ],
)
async def test_live_runtime_evidence_rejects_any_mismatched_observation(manager, ecs_client, monkeypatch, mismatch):
    row = _row()
    task_definition = _task_definition()
    task = _task()
    if mismatch == "row_not_running":
        row["status"] = "provisioning"
    elif mismatch == "task_not_running":
        task["lastStatus"] = "PENDING"
    elif mismatch == "task_stopping":
        task["desiredStatus"] = "STOPPED"
    elif mismatch == "task_arn":
        task["taskArn"] = f"{_TASK_ARN}-other"
    elif mismatch == "task_definition":
        task["taskDefinitionArn"] = f"{_TASK_DEFINITION_ARN}-other"
    elif mismatch == "described_task_definition":
        task_definition["taskDefinition"]["taskDefinitionArn"] = f"{_TASK_DEFINITION_ARN}-other"
    elif mismatch == "service":
        task["group"] = "service:someone-else"
    elif mismatch == "tag":
        task_definition["taskDefinition"]["containerDefinitions"][0]["image"] = _IMAGE.replace(
            "449894a", "old0000"
        )
    elif mismatch == "container_image":
        task["containers"][0]["image"] = _IMAGE.replace("449894a", "old0000")
    elif mismatch == "runtime_digest":
        task["containers"][0]["imageDigest"] = "sha256:" + "7" * 64
    elif mismatch == "private_ip":
        task["attachments"] = []

    _, update_container = _arrange(
        ecs_client,
        monkeypatch,
        row=row,
        task_definition=task_definition,
        task=task,
    )

    assert await manager.live_runtime_evidence(_OWNER_ID) is None
    update_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_runtime_evidence_requires_complete_pinned_identity(manager, ecs_client, monkeypatch):
    get_container, update_container = _arrange(ecs_client, monkeypatch, pin=None)

    assert await manager.live_runtime_evidence(_OWNER_ID) is None
    get_container.assert_not_awaited()
    ecs_client.describe_tasks.assert_not_called()
    update_container.assert_not_awaited()
