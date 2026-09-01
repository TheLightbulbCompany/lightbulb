from unittest.mock import MagicMock, patch

import pytest

from core.services.ops import deploys


@pytest.mark.asyncio
async def test_list_stacks_classifies_and_finds_root_cause():
    fake = MagicMock()
    fake.describe_stacks.return_value = {
        "Stacks": [
            {"StackName": "lightbulb-dev-service", "StackStatus": "UPDATE_ROLLBACK_COMPLETE", "LastUpdatedTime": None},
            {"StackName": "lightbulb-dev-api", "StackStatus": "UPDATE_COMPLETE", "LastUpdatedTime": None},
            {"StackName": "other-stack", "StackStatus": "CREATE_COMPLETE", "LastUpdatedTime": None},
        ]
    }
    # Newest-first events: an IN_PROGRESS stack event marks the operation start.
    fake.describe_stack_events.return_value = {
        "StackEvents": [
            {
                "ResourceType": "AWS::ECS::Service",
                "ResourceStatus": "UPDATE_FAILED",
                "ResourceStatusReason": "task failed to start",
            },
            {
                "ResourceType": "AWS::CloudFormation::Stack",
                "ResourceStatus": "UPDATE_IN_PROGRESS",
                "ResourceStatusReason": "User initiated",
            },
            {
                "ResourceType": "AWS::ECS::Service",
                "ResourceStatus": "UPDATE_FAILED",
                "ResourceStatusReason": "OLD failure from months ago",
            },
        ]
    }
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()

    by_name = {r["stack_name"]: r for r in rows}
    # non-lightbulb stack filtered out
    assert "other-stack" not in by_name
    assert by_name["lightbulb-dev-api"]["root_cause"] is None
    failed = by_name["lightbulb-dev-service"]
    assert failed["status"] == "UPDATE_ROLLBACK_COMPLETE"
    # root cause is the failure WITHIN the current operation window, not the old one
    assert failed["root_cause"] == "task failed to start"


@pytest.mark.asyncio
async def test_root_cause_walks_past_rollback_to_real_failure():
    # Auto-rolled-back stack: the newest-first stream hits the stack's own
    # UPDATE_ROLLBACK_IN_PROGRESS BEFORE the failing resource. The walk must
    # continue past that rollback event, collect the "real cause", and stop at
    # the true UPDATE_IN_PROGRESS operation start — never returning None nor the
    # stale prior-operation "OLD cause".
    fake = MagicMock()
    fake.describe_stacks.return_value = {
        "Stacks": [
            {"StackName": "lightbulb-dev-service", "StackStatus": "UPDATE_ROLLBACK_COMPLETE", "LastUpdatedTime": None},
        ]
    }
    fake.describe_stack_events.return_value = {
        "StackEvents": [
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_ROLLBACK_IN_PROGRESS"},
            {
                "ResourceType": "AWS::ECS::Service",
                "ResourceStatus": "UPDATE_FAILED",
                "ResourceStatusReason": "real cause",
            },
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_IN_PROGRESS"},
            {
                "ResourceType": "AWS::ECS::Service",
                "ResourceStatus": "UPDATE_FAILED",
                "ResourceStatusReason": "OLD cause",
            },
        ]
    }
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()

    assert rows[0]["root_cause"] == "real cause"


@pytest.mark.asyncio
async def test_list_stacks_empty_when_no_lightbulb_stacks():
    fake = MagicMock()
    fake.describe_stacks.return_value = {"Stacks": [{"StackName": "unrelated", "StackStatus": "CREATE_COMPLETE"}]}
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()
    assert rows == []
