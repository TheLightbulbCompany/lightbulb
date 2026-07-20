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
async def test_record_ids_without_claim_raises_and_creates_no_row(paperclip_table):
    """record_onboarding_ids must never fabricate the sentinel row: it may
    only update a row claim_onboarding already created. Without a prior
    claim, the conditional update_item should raise rather than silently
    creating the row from scratch.
    """
    repo = PaperclipRepo(paperclip_table)
    table = repo._table()

    with pytest.raises(table.meta.client.exceptions.ConditionalCheckFailedException):
        await repo.record_onboarding_ids("comp-never-claimed", goal_id="goal-1", project_id="proj-1")

    assert await repo.get_onboarding("comp-never-claimed") is None


@pytest.mark.asyncio
async def test_sentinel_does_not_collide_with_a_user_row(paperclip_table):
    """The synthetic key must not be reachable as a real user_id."""
    repo = PaperclipRepo(paperclip_table)
    await repo.claim_onboarding("comp-1")

    assert await repo.get("comp-1") is None


# ---------------------------------------------------------------------------
# Stale-steal safety net (mirrors try_acquire_org_create_claim's coverage
# in tests/test_paperclip_repo.py: control staleness via stale_after_seconds
# rather than mocking the clock).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_in_flight_claim_is_not_stealable(paperclip_table):
    """A claim that hasn't gone stale yet is a legitimate in-flight seed --
    a second submitter must NOT be able to take it over."""
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    # A generous stale window -> the claim we just took is nowhere near it.
    assert await repo.claim_onboarding("comp-1", stale_after_seconds=120) is False


@pytest.mark.asyncio
async def test_stranded_claim_is_stealable(paperclip_table):
    """A claim whose holder died between claiming and recording (no
    seeded_at) must be stealable once it's past the stale window, or the
    company is locked out of onboarding forever with no self-service
    recovery."""
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    # stale_after_seconds=0 -> any existing claim's age counts as stale.
    assert await repo.claim_onboarding("comp-1", stale_after_seconds=0) is True

    # The steal must leave a fresh, still-claimable-by-record sentinel behind
    # (not just return True) -- record_onboarding_ids should still work.
    await repo.record_onboarding_ids("comp-1", goal_id="goal-1", project_id="proj-1")
    row = await repo.get_onboarding("comp-1")
    assert row["onboarding_goal_id"] == "goal-1"


@pytest.mark.asyncio
async def test_completed_seeded_claim_is_never_stealable(paperclip_table):
    """A claim that already completed the seed (seeded_at set) must never be
    stolen, no matter how old claimed_at is -- a completed seed is not a
    stranded one."""
    repo = PaperclipRepo(paperclip_table)

    assert await repo.claim_onboarding("comp-1") is True
    await repo.record_onboarding_ids("comp-1", goal_id="goal-1", project_id="proj-1")

    # Even with a zero stale window (which would steal any unseeded claim),
    # a seeded claim must stay put.
    assert await repo.claim_onboarding("comp-1", stale_after_seconds=0) is False
