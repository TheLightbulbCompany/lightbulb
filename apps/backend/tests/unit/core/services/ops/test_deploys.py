from unittest.mock import MagicMock, patch

import pytest

from core.services.ops import deploys


@pytest.mark.asyncio
async def test_list_stacks_classifies_and_finds_root_cause():
    fake = MagicMock()
    fake.describe_stacks.return_value = {
        "Stacks": [
            {"StackName": "isol8-dev-service", "StackStatus": "UPDATE_ROLLBACK_COMPLETE", "LastUpdatedTime": None},
            {"StackName": "isol8-dev-api", "StackStatus": "UPDATE_COMPLETE", "LastUpdatedTime": None},
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
    # non-isol8 stack filtered out
    assert "other-stack" not in by_name
    assert by_name["isol8-dev-api"]["root_cause"] is None
    failed = by_name["isol8-dev-service"]
    assert failed["status"] == "UPDATE_ROLLBACK_COMPLETE"
    # root cause is the failure WITHIN the current operation window, not the old one
    assert failed["root_cause"] == "task failed to start"


@pytest.mark.asyncio
async def test_list_stacks_empty_when_no_isol8_stacks():
    fake = MagicMock()
    fake.describe_stacks.return_value = {"Stacks": [{"StackName": "unrelated", "StackStatus": "CREATE_COMPLETE"}]}
    with patch.object(deploys, "_cfn", return_value=fake), patch.object(deploys.settings, "ENVIRONMENT", "dev"):
        rows = await deploys.list_stacks()
    assert rows == []
