"""Tests for the company-deploy job repository (moto-backed).

Covers the job-ledger seam consumed by the deploy engine (Tasks 4/5): a
job is minted in `running` state with one `pending` agent entry per plan
slug, each agent's paperclip_id/state is recorded as it materializes, the
overall status flips (with an optional error), and get_job round-trips.
"""

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.repositories import company_deploy_repo  # noqa: E402


NOW = "2026-07-18T00:00:00Z"
LATER = "2026-07-18T01:00:00Z"

_DEPLOYS = "test-marketplace-company-deploys"


@pytest.fixture
def deploys_table():
    """Moto table with the prod key schema (PK job_id S, no sort key)."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName=_DEPLOYS,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.meta.client.get_waiter("table_exists").wait(TableName=_DEPLOYS)
        with patch.object(settings, "MARKETPLACE_COMPANY_DEPLOYS_TABLE", _DEPLOYS):
            yield table


async def _create() -> str:
    return await company_deploy_repo.create_job(
        owner_id="owner_1",
        listing_id="listing_1",
        listing_version=3,
        company_slug="acme",
        agent_slugs=["scout", "closer"],
        now_iso=NOW,
    )


# ---------------------------------------------------------------------------
# create_job / get_job round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_writes_running_row_with_seeded_agents(deploys_table):
    job_id = await _create()
    assert job_id.startswith("cdpl_")

    job = await company_deploy_repo.get_job(job_id)
    assert job is not None
    assert job["owner_id"] == "owner_1"
    assert job["listing_id"] == "listing_1"
    assert int(job["listing_version"]) == 3
    assert job["company_slug"] == "acme"
    assert job["status"] == "running"
    assert job["error"] is None
    assert job["routine_ids"] == []
    assert job["created_at"] == NOW
    assert job["updated_at"] == NOW

    # One pending agent entry per plan slug, preserving order.
    assert job["agents"] == [
        {"slug": "scout", "paperclip_id": None, "state": "pending"},
        {"slug": "closer", "paperclip_id": None, "state": "pending"},
    ]


@pytest.mark.asyncio
async def test_create_job_mints_unique_ids(deploys_table):
    first = await _create()
    second = await _create()
    assert first != second


@pytest.mark.asyncio
async def test_get_job_missing_returns_none(deploys_table):
    assert await company_deploy_repo.get_job("cdpl_nope") is None


# ---------------------------------------------------------------------------
# record_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_agent_updates_matching_entry_only(deploys_table):
    job_id = await _create()

    await company_deploy_repo.record_agent(
        job_id=job_id,
        slug="closer",
        paperclip_id="pc_closer_99",
        state="paired",
        now_iso=LATER,
    )

    job = await company_deploy_repo.get_job(job_id)
    by_slug = {a["slug"]: a for a in job["agents"]}
    assert by_slug["closer"] == {"slug": "closer", "paperclip_id": "pc_closer_99", "state": "paired"}
    # The other agent is untouched.
    assert by_slug["scout"] == {"slug": "scout", "paperclip_id": None, "state": "pending"}
    assert job["updated_at"] == LATER


@pytest.mark.asyncio
async def test_record_agent_can_flip_to_failed(deploys_table):
    job_id = await _create()

    await company_deploy_repo.record_agent(
        job_id=job_id,
        slug="scout",
        paperclip_id=None,
        state="failed",
        now_iso=LATER,
    )

    job = await company_deploy_repo.get_job(job_id)
    by_slug = {a["slug"]: a for a in job["agents"]}
    assert by_slug["scout"]["state"] == "failed"
    assert by_slug["scout"]["paperclip_id"] is None


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_completed(deploys_table):
    job_id = await _create()

    await company_deploy_repo.set_status(job_id=job_id, status="completed", now_iso=LATER)

    job = await company_deploy_repo.get_job(job_id)
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["updated_at"] == LATER


@pytest.mark.asyncio
async def test_set_status_failed_records_error(deploys_table):
    job_id = await _create()

    await company_deploy_repo.set_status(
        job_id=job_id,
        status="failed",
        now_iso=LATER,
        error="materialize threw",
    )

    job = await company_deploy_repo.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error"] == "materialize threw"


# ---------------------------------------------------------------------------
# accessor idiom — table name comes from settings
# ---------------------------------------------------------------------------


def test_table_accessor_reads_settings_table_name():
    fake_resource = MagicMock()
    with (
        patch.object(settings, "MARKETPLACE_COMPANY_DEPLOYS_TABLE", "some-table"),
        patch.object(boto3, "resource", return_value=fake_resource) as res,
    ):
        company_deploy_repo._table()
    res.assert_called_once_with("dynamodb")
    fake_resource.Table.assert_called_once_with("some-table")
