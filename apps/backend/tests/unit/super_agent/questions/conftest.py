"""A moto `tasks` table built from `tasks_schema` (keys + GSIs), shared by the lane-Q repo suites."""

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts

WS = "org_1"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


@pytest.fixture
def table(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
        t = resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": g.name,
                    "KeySchema": [
                        {"AttributeName": g.pk_attr, "KeyType": "HASH"},
                        {"AttributeName": g.sk_attr, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for g in ts.GSIS
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        # The task the lane-Q suites ask questions on (opening one adds its answerers to the task's audience).
        t.put_item(
            Item=ts.Task(
                ws=WS,
                taskId=TASK,
                identifier="LB-1",
                title="Register the domain",
                status="waiting",
                audience={"kind": "person", "userIds": ["user_sam"]},
                lane="private",
                ownerUserId="user_sam",
                triggerId=ts.trigger_id("chat", "m1"),
                createdAt="2026-11-04T00:00:00+00:00",
                updatedAt="2026-11-04T00:00:00+00:00",
            ).to_item()
        )
        yield t
