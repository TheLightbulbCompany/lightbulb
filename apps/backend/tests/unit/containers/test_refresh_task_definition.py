"""Tests for EcsManager.refresh_task_definition.

Surgically re-registers a user's task definition with the current env
list (so OPENCLAW_HOOK_TOKEN backfilled mid-life is applied) and rolls
the running task onto the new revision. Does NOT touch the EFS access
point — losing agent state would be a backfill regression.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_refresh_registers_new_taskdef_with_hook_token_env(monkeypatch):
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()

    fake_existing_container = {
        "owner_id": "u_1",
        "service_name": "openclaw-u-1-abc",
        "hook_token": "freshly-set",
        "task_definition_arn": ("arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:4"),
    }

    fake_ecs = MagicMock()
    fake_ecs.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": ("arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:5")
        }
    }
    fake_ecs.update_service.return_value = {}
    fake_ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"name": "openclaw", "environment": [], "secrets": []}],
            "family": "isol8-dev-openclaw-u-1",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "512",
            "memory": "1024",
            "executionRoleArn": "arn:aws:iam::877352799272:role/exec",
            "taskRoleArn": "arn:aws:iam::877352799272:role/task",
            "volumes": [],
        }
    }

    with (
        patch("core.repositories.container_repo.get_by_owner_id", new=AsyncMock(return_value=fake_existing_container)),
        patch("core.repositories.container_repo.update_fields", new=AsyncMock()),
        # The ECS boto3 client lives on `self._ecs` (not `_ecs_client`).
        patch.object(manager, "_ecs", fake_ecs),
    ):
        result = await manager.refresh_task_definition("u_1")

    # Verify register was called with an env list including OPENCLAW_HOOK_TOKEN.
    register_kwargs = fake_ecs.register_task_definition.call_args.kwargs
    cd = register_kwargs["containerDefinitions"][0]
    env_names = {e["name"] for e in cd["environment"]}
    assert "OPENCLAW_HOOK_TOKEN" in env_names
    token_entry = next(e for e in cd["environment"] if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "freshly-set"

    # Verify the service was updated to the new revision.
    fake_ecs.update_service.assert_called_once()
    upd_kwargs = fake_ecs.update_service.call_args.kwargs
    assert upd_kwargs["taskDefinition"].endswith(":5")

    assert result == "arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:5"


@pytest.mark.asyncio
async def test_refresh_returns_none_when_no_container_row(monkeypatch):
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()
    with patch("core.repositories.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)):
        result = await manager.refresh_task_definition("u_unknown")
    assert result is None


@pytest.mark.asyncio
async def test_refresh_returns_none_when_row_missing_taskdef_arn(monkeypatch):
    """Defensive: if a row exists but has no task_definition_arn (legacy/zombie
    row), refresh should return None rather than crash on a missing key."""
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()
    with patch(
        "core.repositories.container_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "owner_id": "u_zombie",
                "service_name": "stale",
                # no task_definition_arn
            }
        ),
    ):
        result = await manager.refresh_task_definition("u_zombie")
    assert result is None
