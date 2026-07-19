"""Onboarding sentinel: one-shot per COMPANY, not per user.

Exercised against a REAL DynamoDB engine (moto) rather than a hand mock —
the claim is a conditional write and a mock cannot tell us whether
DynamoDB would actually reject the second one.
"""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.repositories.paperclip_repo import PaperclipRepo

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def paperclip_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-paperclip-companies",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-paperclip-companies")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield "paperclip-companies"


@pytest.mark.asyncio
async def test_first_claim_wins_second_loses(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    assert await repo.claim_onboarding("comp-1") is False


@pytest.mark.asyncio
async def test_claim_is_scoped_per_company(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    assert await repo.claim_onboarding("comp-2") is True


@pytest.mark.asyncio
async def test_release_allows_reclaim(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    await repo.release_onboarding_claim("comp-1")
    assert await repo.claim_onboarding("comp-1") is True


@pytest.mark.asyncio
async def test_record_ids_then_read_back(paperclip_table):
    repo = PaperclipRepo(paperclip_table)
    await repo.claim_onboarding("comp-1")

    await repo.record_onboarding_ids("comp-1", goal_id="goal-1", project_id="proj-1")

    row = await repo.get_onboarding("comp-1")
    assert row["onboarding_goal_id"] == "goal-1"
    assert row["onboarding_project_id"] == "proj-1"


@pytest.mark.asyncio
async def test_get_onboarding_absent_returns_none(paperclip_table):
    repo = PaperclipRepo(paperclip_table)

    assert await repo.get_onboarding("comp-nope") is None


@pytest.mark.asyncio
async def test_sentinel_does_not_collide_with_a_user_row(paperclip_table):
    """The synthetic key must not be reachable as a real user_id."""
    repo = PaperclipRepo(paperclip_table)
    await repo.claim_onboarding("comp-1")

    assert await repo.get("comp-1") is None
