"""refresh_task_definition(extra_secrets=) + _build_slack_secrets_for_user."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import ecs_manager as em  # noqa: E402


def _manager():
    m = em.EcsManager.__new__(em.EcsManager)
    m._ecs = MagicMock()
    return m


def _task_def():
    return {
        "taskDefinition": {
            "family": "fam",
            "networkMode": "awsvpc",
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "environment": [{"name": "OPENCLAW_HOOK_TOKEN", "value": "old"}],
                    "secrets": [{"name": "OPENAI_API_KEY", "valueFrom": "arn:byok"}],
                }
            ],
        }
    }


def test_slack_env_var_name_is_uppercase_team():
    assert em.slack_env_var_name("t0123abc") == "SLACK_BOT_TOKEN_T0123ABC"


@pytest.mark.asyncio
async def test_refresh_merges_extra_secrets_replacing_same_name():
    m = _manager()
    m._ecs.describe_task_definition.return_value = _task_def()
    m._ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:new"}}
    container = {"task_definition_arn": "arn:old", "hook_token": "hk", "service_name": "svc"}
    with (
        patch.object(em.container_repo, "get_by_owner_id", AsyncMock(return_value=container)),
        patch.object(em.container_repo, "update_fields", AsyncMock()),
        patch.object(em.settings, "ECS_CLUSTER_ARN", "arn:cluster"),
    ):
        arn = await m.refresh_task_definition(
            "org_1",
            extra_secrets=[
                {"name": "SLACK_BOT_TOKEN_T1", "valueFrom": "arn:slack1"},
                {"name": "OPENAI_API_KEY", "valueFrom": "arn:byok-new"},
            ],
        )
    assert arn == "arn:new"
    registered = m._ecs.register_task_definition.call_args.kwargs["containerDefinitions"][0]["secrets"]
    assert {s["name"]: s["valueFrom"] for s in registered} == {
        "OPENAI_API_KEY": "arn:byok-new",
        "SLACK_BOT_TOKEN_T1": "arn:slack1",
    }


@pytest.mark.asyncio
async def test_refresh_without_extra_secrets_keeps_existing():
    m = _manager()
    m._ecs.describe_task_definition.return_value = _task_def()
    m._ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:new"}}
    container = {"task_definition_arn": "arn:old", "hook_token": "hk", "service_name": "svc"}
    with (
        patch.object(em.container_repo, "get_by_owner_id", AsyncMock(return_value=container)),
        patch.object(em.container_repo, "update_fields", AsyncMock()),
        patch.object(em.settings, "ECS_CLUSTER_ARN", "arn:cluster"),
    ):
        await m.refresh_task_definition("org_1")
    registered = m._ecs.register_task_definition.call_args.kwargs["containerDefinitions"][0]["secrets"]
    assert registered == [{"name": "OPENAI_API_KEY", "valueFrom": "arn:byok"}]


@pytest.mark.asyncio
async def test_build_slack_secrets_lists_active_installs_only():
    m = _manager()
    rows = [
        {"team_id": "T1", "status": "active", "token_secret_name": "lightbulb/dev/integrations/org_1/slack/T1"},
        {"team_id": "T2", "status": "revoked", "token_secret_name": "lightbulb/dev/integrations/org_1/slack/T2"},
    ]
    with (
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)),
        patch("core.containers.ecs_manager._secret_arn_for_name", AsyncMock(side_effect=lambda n: f"arn:{n}")),
    ):
        out = await m._build_slack_secrets_for_user("org_1")
    assert out == [{"name": "SLACK_BOT_TOKEN_T1", "valueFrom": "arn:lightbulb/dev/integrations/org_1/slack/T1"}]


@pytest.mark.asyncio
async def test_build_slack_secrets_is_best_effort():
    m = _manager()
    with patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await m._build_slack_secrets_for_user("org_1") == []
