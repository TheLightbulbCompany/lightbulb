"""Shared fixtures for service-level tests.

Most service tests mock at the boto3 call boundary (MagicMock the
table accessor). A few tests exercise multi-step flows where the
behavior under test depends on real DynamoDB semantics — conditional
puts, query ordering by sort key, GSI projection, slug-reservation
rows. For those, mocking would either hardcode the responses (which
defeats the point) or grow a re-implementation of the conditional-put
semantics.

This file owns the moto-backed fixtures shared across service tests.
New moto-backed service tests should pull from here.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402


@pytest.fixture(autouse=True)
def _no_agent_gate_bearer(monkeypatch):
    """Profile writes mint a per-agent gate bearer from the containers row; service tests that
    write profiles mock the stores around them, not that lookup. Tests of the bearer itself live
    in tests/unit/billing/test_agent_gate_bearer.py."""
    monkeypatch.setattr("core.services.hermes_provisioning.agent_gate_bearer", AsyncMock(return_value=None))


@pytest.fixture
def repos():
    """Moto-backed messaging table (pk/sk) wired into messaging_repo and the A2A repo."""
    table_name = "test-messaging"
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=table_name)
        from core.config import settings

        with (
            patch("core.dynamodb._dynamodb_resource", ddb),
            patch.object(settings, "MESSAGING_TABLE", table_name),
        ):
            yield ddb
