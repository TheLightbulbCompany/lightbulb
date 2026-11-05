"""A moto `tasks` table built from `tasks_schema` (keys + GSIs), shared by the lane-Q repo suites."""

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts


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
        yield t
