from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services import aws_eventbridge


def _session(events=None, cloudwatch=None):
    session = MagicMock()
    session.client.side_effect = lambda service, **_kwargs: {
        "events": events,
        "cloudwatch": cloudwatch,
    }[service]
    return session


@pytest.mark.asyncio
async def test_list_alarm_resources_returns_all_and_sorted_alarms():
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "MetricAlarms": [
                {"AlarmName": "Zeta", "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:Zeta"},
                {"AlarmName": "alpha", "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:alpha"},
            ],
            "CompositeAlarms": [{"AlarmName": "Beta", "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:Beta"}],
        }
    ]
    cloudwatch = MagicMock()
    cloudwatch.get_paginator.return_value = paginator
    session = _session(cloudwatch=cloudwatch)

    with patch.object(
        aws_eventbridge,
        "_connected_session",
        AsyncMock(return_value=(session, "us-east-1", "aws", "123456789012")),
    ):
        rows = await aws_eventbridge.list_alarm_resources("owner_1")

    assert [row["label"] for row in rows] == [
        "All CloudWatch alarms in us-east-1",
        "alpha",
        "Beta",
        "Zeta",
    ]
    paginator.paginate.assert_called_once_with(AlarmTypes=["MetricAlarm", "CompositeAlarm"])


def test_register_sync_builds_alarm_only_destination():
    events = MagicMock()
    events.create_connection.return_value = {
        "ConnectionArn": "arn:aws:events:us-east-1:123456789012:connection/x/1",
        "ConnectionState": "AUTHORIZED",
    }
    events.create_api_destination.return_value = {
        "ApiDestinationArn": "arn:aws:events:us-east-1:123456789012:api-destination/x/1"
    }
    events.put_targets.return_value = {"FailedEntryCount": 0}
    session = _session(events=events)
    names = aws_eventbridge._names("owner_1", "pub_1")

    aws_eventbridge._register_sync(
        session,
        region="us-east-1",
        partition="aws",
        account_id="123456789012",
        endpoint="https://api.isol8.co/api/v1/hooks/t/owner_1/pub_1",
        webhook_secret="pcsec_1",
        alarm_resource="arn:aws:cloudwatch:us-east-1:123456789012:alarm:api-errors",
        names=names,
    )

    auth = events.create_connection.call_args.kwargs["AuthParameters"]["ApiKeyAuthParameters"]
    assert auth == {"ApiKeyName": "Authorization", "ApiKeyValue": "Bearer pcsec_1"}
    pattern = events.put_rule.call_args.kwargs["EventPattern"]
    assert '"value":["ALARM"]' in pattern
    assert '"resources":["arn:aws:cloudwatch:us-east-1:123456789012:alarm:api-errors"]' in pattern
    target = events.put_targets.call_args.kwargs["Targets"][0]
    assert target["RoleArn"] == "arn:aws:iam::123456789012:role/isol8-eventbridge-api-destination"
    assert target["RetryPolicy"] == {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}


def test_register_sync_rolls_back_a_rejected_target():
    events = MagicMock()
    events.create_connection.return_value = {
        "ConnectionArn": "arn:connection",
        "ConnectionState": "AUTHORIZED",
    }
    events.create_api_destination.return_value = {"ApiDestinationArn": "arn:destination"}
    events.put_targets.return_value = {"FailedEntryCount": 1}
    session = _session(events=events)

    with pytest.raises(RuntimeError, match="rejected"):
        aws_eventbridge._register_sync(
            session,
            region="us-east-1",
            partition="aws",
            account_id="123456789012",
            endpoint="https://api.isol8.co/hook",
            webhook_secret="pcsec_1",
            alarm_resource="*",
            names=aws_eventbridge._names("owner_1", "pub_1"),
        )

    events.remove_targets.assert_called_once()
    events.delete_rule.assert_called_once()
    events.delete_api_destination.assert_called_once()
    events.delete_connection.assert_called_once_with(Name=aws_eventbridge._names("owner_1", "pub_1")["connection_name"])


@pytest.mark.asyncio
async def test_register_permission_error_is_caller_facing():
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateConnection")
    session = MagicMock()
    with (
        patch.object(
            aws_eventbridge,
            "list_alarm_resources",
            AsyncMock(return_value=[{"id": "*", "label": "All CloudWatch alarms"}]),
        ),
        patch.object(
            aws_eventbridge,
            "_connected_session",
            AsyncMock(return_value=(session, "us-east-1", "aws", "123456789012")),
        ),
        patch.object(aws_eventbridge, "run_in_thread", AsyncMock(side_effect=denied)),
        pytest.raises(aws_eventbridge.AwsEventBridgePermissionError, match="PowerUser"),
    ):
        await aws_eventbridge.register_alarm_destination(
            "owner_1",
            trigger_public_id="pub_1",
            endpoint="https://api.isol8.co/hook",
            webhook_secret="pcsec_1",
            alarm_resource="*",
        )


@pytest.mark.asyncio
async def test_register_rejects_alarm_outside_connected_account():
    with (
        patch.object(
            aws_eventbridge,
            "list_alarm_resources",
            AsyncMock(return_value=[{"id": "*", "label": "All CloudWatch alarms"}]),
        ),
        pytest.raises(ValueError, match="not in your connected AWS account"),
    ):
        await aws_eventbridge.register_alarm_destination(
            "owner_1",
            trigger_public_id="pub_1",
            endpoint="https://api.isol8.co/hook",
            webhook_secret="pcsec_1",
            alarm_resource="arn:not-owned",
        )
