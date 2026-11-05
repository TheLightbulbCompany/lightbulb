"""A moto `tasks` table built from `tasks_schema` (keys + GSIs) and a metric recorder for the watchdog suites."""

import boto3
import pytest
import pytest_asyncio
from moto import mock_aws

from core import dynamodb, tasks_schema as ts

WS = "org_1"


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


@pytest.fixture
def metrics(monkeypatch):
    """Every `put_metric` call made through a watchdog module, as (name, value, dimensions)."""
    seen: list[tuple[str, float, dict]] = []

    def record(name, value=1.0, unit="Count", dimensions=None):
        seen.append((name, value, dimensions or {}))

    import core.repositories.watchdog_repo as repo

    monkeypatch.setattr(repo, "put_metric", record)
    try:
        import core.services.watchdog as wd

        monkeypatch.setattr(wd, "put_metric", record)
    except ImportError:
        pass
    return seen


@pytest_asyncio.fixture
async def temporal_env():
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()
