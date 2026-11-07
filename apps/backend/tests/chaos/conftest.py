"""Chaos suite fixtures: moto-backed DynamoDB + S3 with all isol8 tables, and
hypothesis profiles.

Table names are derived two different ways in production code, so the
fixture wires both:
  * ``core.dynamodb.get_table`` (prefix convention) — usage-counters,
    billing-accounts, paperclip-companies. Patched via ``_table_prefix`` +
    ``_dynamodb_resource``.
  * ``os.environ["WEBHOOK_DEDUP_TABLE"]`` — webhook_dedup makes its own boto3
    resource (moto intercepts it inside the ``mock_aws`` context).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "wHc3hAOcLlFzWyu3Ph7xIyClIdVQTrIzFOZDtu_pIEY=")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import pytest
from hypothesis import HealthCheck, settings

# --- Hypothesis profiles -----------------------------------------------------
# `ci`  : bounded, deadline-guarded — runs on every PR.
# `dev` : deep, no deadline — local "really try to break it" runs.
_HEALTH = [HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
settings.register_profile("ci", max_examples=50, deadline=None, suppress_health_check=_HEALTH)
settings.register_profile("dev", max_examples=500, deadline=None, suppress_health_check=_HEALTH)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

_PREFIX = "test-"
_DEDUP_TABLE = "test-webhook-event-dedup"


def _create_tables(resource):
    """Create every table the chaos machines touch, matching CDK key schemas
    (apps/infra/lib/stacks/database-stack.ts)."""
    waiters = []

    def _make(name, key_schema, attr_defs, gsis=None):
        kwargs = dict(
            TableName=name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",
        )
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = gsis
        t = resource.create_table(**kwargs)
        waiters.append(name)
        return t

    # billing-accounts: PK owner_id, GSIs stripe-customer-index +
    # apple-original-transaction-index (mirror database-stack.ts exactly — a
    # fixture missing a prod GSI cannot catch empty-string-on-index writes).
    _make(
        f"{_PREFIX}billing-accounts",
        [{"AttributeName": "owner_id", "KeyType": "HASH"}],
        [
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
            {"AttributeName": "apple_original_transaction_id", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "stripe-customer-index",
                "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "apple-original-transaction-index",
                "KeySchema": [{"AttributeName": "apple_original_transaction_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    # usage-counters: PK owner_id, SK period
    _make(
        f"{_PREFIX}usage-counters",
        [
            {"AttributeName": "owner_id", "KeyType": "HASH"},
            {"AttributeName": "period", "KeyType": "RANGE"},
        ],
        [
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "period", "AttributeType": "S"},
        ],
    )
    # paperclip-companies: PK user_id, GSI by-org-id (PK org_id)
    _make(
        f"{_PREFIX}paperclip-companies",
        [{"AttributeName": "user_id", "KeyType": "HASH"}],
        [
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "org_id", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "by-org-id",
                "KeySchema": [{"AttributeName": "org_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    # oauth-state: PK state — the PUBLIC Stripe Connect callback consumes it,
    # so the fuzzer must reach the real "unknown state" branch, not a 5xx.
    _make(
        f"{_PREFIX}oauth-state",
        [{"AttributeName": "state", "KeyType": "HASH"}],
        [{"AttributeName": "state", "AttributeType": "S"}],
    )
    # setup-requests: PK id — the PUBLIC /request-setup form writes a row once
    # the body is valid, so the fuzzer needs the table to reach a 2xx.
    _make(
        f"{_PREFIX}setup-requests",
        [{"AttributeName": "id", "KeyType": "HASH"}],
        [{"AttributeName": "id", "AttributeType": "S"}],
    )
    # containers: PK owner_id + status-index — the container hook routes read
    # the owner's hook_token before anything else.
    _make(
        f"{_PREFIX}containers",
        [{"AttributeName": "owner_id", "KeyType": "HASH"}],
        [
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "status-index",
                "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    # webhook-event-dedup: PK event_id
    _make(
        _DEDUP_TABLE,
        [{"AttributeName": "event_id", "KeyType": "HASH"}],
        [{"AttributeName": "event_id", "AttributeType": "S"}],
    )
    # integrations: PK owner_id, SK provider (secret-free connection-status
    # index). Touched by the unauthenticated aws credential_process endpoint
    # (POST /connections/aws/credentials), which reads the status row before
    # the Secrets Manager token check — so the negative pass needs it present
    # to reach the 401 instead of a missing-table 5xx.
    _make(
        f"{_PREFIX}integrations",
        [
            {"AttributeName": "owner_id", "KeyType": "HASH"},
            {"AttributeName": "provider", "KeyType": "RANGE"},
        ],
        [
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "provider", "AttributeType": "S"},
        ],
    )
    # provisioning-workflows: PK workflow_id, GSI owner-state-index (mirror
    # database-stack.ts exactly).
    _make(
        f"{_PREFIX}provisioning-workflows",
        [{"AttributeName": "workflow_id", "KeyType": "HASH"}],
        [
            {"AttributeName": "workflow_id", "AttributeType": "S"},
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "state", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "owner-state-index",
                "KeySchema": [
                    {"AttributeName": "owner_id", "KeyType": "HASH"},
                    {"AttributeName": "state", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )

    for name in waiters:
        resource.meta.client.get_waiter("table_exists").wait(TableName=name)


@pytest.fixture
def chaos_ddb(monkeypatch):
    """Boot moto (DynamoDB + S3) with every table the chaos machines
    touch, wired into both table-name mechanisms."""
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(resource)

        # 1. prefix-convention repos (usage, billing, paperclip)
        monkeypatch.setattr("core.dynamodb._table_prefix", _PREFIX)
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", resource)
        # 2. webhook_dedup reads this env var lazily
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", _DEDUP_TABLE)

        yield resource
