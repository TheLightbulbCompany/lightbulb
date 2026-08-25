"""EcsManager._deployed_image_tag — the image tag fork-gated config keys are
written against (see openclaw_version.deployed_isol8_patch_at_least)."""

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
                    "image": "1.dkr.ecr.us-east-1.amazonaws.com/isol8/openclaw-extended:2026.7.2-isol8.8-8f700f0",
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
