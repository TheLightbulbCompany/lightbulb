"""Lifecycle records for AWS API Destinations remain secret-free."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_returns_empty_when_aws_row_is_missing():
    from core.repositories import integrations_repo

    with patch.object(integrations_repo, "get", AsyncMock(return_value=None)):
        assert await integrations_repo.get_aws_eventbridge_destinations("owner_1") == []


@pytest.mark.asyncio
async def test_set_updates_only_destination_metadata():
    from core.repositories import integrations_repo

    table = AsyncMock()
    with (
        patch.object(integrations_repo, "_table", return_value=table),
        patch.object(integrations_repo, "run_in_thread", AsyncMock()) as run,
    ):
        await integrations_repo.set_aws_eventbridge_destinations(
            "owner_1",
            [
                {
                    "connection_name": "isol8-a-connection",
                    "destination_name": "isol8-a-destination",
                    "rule_name": "isol8-a-alarm",
                    "region": "us-east-1",
                    "resource": "*",
                    "trigger_public_id": "pub_1",
                }
            ],
        )

    values = run.await_args.kwargs["ExpressionAttributeValues"]
    assert "webhook_secret" not in repr(values)
    assert values[":destinations"][0]["trigger_public_id"] == "pub_1"
