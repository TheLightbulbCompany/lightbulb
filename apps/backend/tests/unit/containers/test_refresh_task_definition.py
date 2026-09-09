"""Tests for EcsManager.refresh_task_definition.

Surgically re-registers a user's task definition with the current env
list (so OPENCLAW_HOOK_TOKEN backfilled mid-life is applied) and rolls
the running task onto the new revision. Does NOT touch the EFS access
point — losing agent state would be a backfill regression.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _lifecycle_guard_stub(monkeypatch):
    @asynccontextmanager
    async def unlocked(_owner_id, **_kwargs):
        yield {}

    monkeypatch.setattr("core.services.openclaw_release.owner_lifecycle_guard", unlocked)


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
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "image": "legacy.example/openclaw:founder-canary",
                    "environment": [],
                    "secrets": [],
                }
            ],
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
    assert cd["image"] == "legacy.example/openclaw:founder-canary"
    assert [e for e in cd["environment"] if e["name"] == "ISOL8_CONFIG_LOCK_PROTOCOL"] == [
        {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "guard-v1"}
    ]

    # Verify the service was updated to the new revision.
    fake_ecs.update_service.assert_called_once()
    upd_kwargs = fake_ecs.update_service.call_args.kwargs
    assert upd_kwargs["taskDefinition"].endswith(":5")

    assert result == "arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:5"


@pytest.mark.asyncio
async def test_refresh_materializes_assigned_release_digest_with_desired_absent(monkeypatch):
    from core.containers import ecs_manager
    from core.services import openclaw_release

    manager = ecs_manager.EcsManager()
    digest_image = "877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended@sha256:" + "a" * 64
    container = {
        "owner_id": "u_1",
        "service_name": "openclaw-u-1-abc",
        "hook_token": "freshly-set",
        "task_definition_arn": "arn:task-definition/openclaw:4",
        "active_release_id": "founder-canary",
        "release_revision": 1,
    }
    fake_ecs = MagicMock()
    fake_ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "image": "mutable.example/openclaw:latest",
                    "environment": [],
                    "secrets": [],
                }
            ],
            "family": "openclaw-user",
            "networkMode": "awsvpc",
        }
    }
    fake_ecs.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:task-definition/openclaw:5"}
    }

    with (
        patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=container)),
        patch("core.repositories.container_repo.update_fields", AsyncMock()),
        patch.object(openclaw_release, "release_by_id", return_value=SimpleNamespace(digest_image=digest_image)),
        patch.object(manager, "_ecs", fake_ecs),
    ):
        await manager.refresh_task_definition("u_1")

    registered = fake_ecs.register_task_definition.call_args.kwargs["containerDefinitions"]
    assert next(item for item in registered if item["name"] == "openclaw")["image"] == digest_image


@pytest.mark.asyncio
async def test_refresh_strips_stale_paperclip_env_keeps_others(monkeypatch):
    """A task-def registered before per-agent Paperclip identity carries the old
    container-wide PAPERCLIP_* Friday env. refresh_task_definition must drop
    every PAPERCLIP_* entry (so non-Friday agents can't resolve Friday's shared
    env) while preserving unrelated env (CODEX_HOME, etc.) and re-injecting the
    fresh OPENCLAW_HOOK_TOKEN."""
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()

    fake_existing_container = {
        "owner_id": "u_1",
        "service_name": "openclaw-u-1-abc",
        "hook_token": "freshly-set",
        "task_definition_arn": ("arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:4"),
    }

    stale_env = [
        {"name": "PAPERCLIP_API_KEY", "value": "friday-key"},
        {"name": "PAPERCLIP_AGENT_ID", "value": "friday-uuid"},
        {"name": "PAPERCLIP_COMPANY_ID", "value": "co_x"},
        {"name": "PAPERCLIP_API_URL", "value": "http://paperclip.internal"},
        {"name": "CODEX_HOME", "value": "/mnt/efs/codex"},
        {"name": "OPENCLAW_HOOK_TOKEN", "value": "stale-token"},
        {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "stale-value"},
    ]

    fake_ecs = MagicMock()
    fake_ecs.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": ("arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:5")
        }
    }
    fake_ecs.update_service.return_value = {}
    fake_ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"name": "openclaw", "environment": stale_env, "secrets": []}],
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
        patch.object(manager, "_ecs", fake_ecs),
    ):
        await manager.refresh_task_definition("u_1")

    register_kwargs = fake_ecs.register_task_definition.call_args.kwargs
    cd = register_kwargs["containerDefinitions"][0]
    env_names = {e["name"] for e in cd["environment"]}

    # Every legacy PAPERCLIP_* var is gone.
    assert not any(n.startswith("PAPERCLIP_") for n in env_names)
    # Unrelated per-user env survives untouched.
    codex = next(e for e in cd["environment"] if e["name"] == "CODEX_HOME")
    assert codex["value"] == "/mnt/efs/codex"
    # The hook token is re-injected with the fresh value (not the stale one).
    token_entry = next(e for e in cd["environment"] if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "freshly-set"
    assert [e for e in cd["environment"] if e["name"] == "ISOL8_CONFIG_LOCK_PROTOCOL"] == [
        {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "guard-v1"}
    ]


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
