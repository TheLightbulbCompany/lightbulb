"""Lane R fixtures: a moto `tasks` table built from `tasks_schema` (keys + GSIs) and a Paperclip roster.

`paperclip_roster.json` has the shape `GET /api/companies/{id}/agents` returns for a provisioned workspace (the
six starters from `workspace_provisioning.STARTER_ROSTER`, one marketplace hire, one terminated agent), ids fresh
UUIDs, personas placeholder text."""

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from core import dynamodb, tasks_schema as ts


def _create_tasks_table(resource):
    attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
    return resource.create_table(
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


@pytest.fixture
def ddb(monkeypatch):
    """moto DynamoDB with no tables; `tasks_table` adds the `tasks` table."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield resource


@pytest.fixture
def tasks_table(ddb):
    return _create_tasks_table(ddb)


@pytest.fixture
def paperclip_roster():
    return json.loads((Path(__file__).parent / "paperclip_roster.json").read_text())


def registry_row(agent: dict) -> dict:
    """A `paperclip_roster.json` agent as its `AGENT#` row fields."""
    meta = agent.get("metadata") or {}
    listing = {
        k: v
        for k, v in (("listingId", meta.get("isol8_listing_id")), ("listingSlug", meta.get("isol8_listing_slug")))
        if v
    }
    return {
        "agentId": agent["id"],
        "name": str(agent.get("name") or agent["id"])[:256],
        "role": agent.get("role"),
        "title": agent.get("title"),
        "persona": agent.get("capabilities"),
        "icon": agent.get("icon"),
        "appearance": agent.get("appearance"),
        "reportsTo": agent.get("reportsTo"),
        "status": "paused" if agent.get("status") == "paused" else "active",
        "listing": listing or None,
        "budgetMonthlyCents": int(agent.get("budgetMonthlyCents") or 0),
        "companyId": agent.get("companyId"),
    }
