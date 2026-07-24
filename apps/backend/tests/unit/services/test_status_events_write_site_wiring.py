"""Pins the durable write sites that ride publish_status_changed (REALTIME,
spec PR-7): every repository write that can move a fact compute_status
composes must fire the realtime nudge right after the write commits.

Exercised against real DynamoDB engines (moto) for the conditional-write
repos (container_repo, PaperclipRepo) and a patched table for the plain
put-based billing_repo writers, with publish_status_changed itself mocked
at each repo module's imported name -- this file only pins THAT it is
called (and with the right owner_id), not its own internals (covered by
test_status_events.py and test_connection_pool_broadcast_to_owner.py).
"""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# containers table -- container_repo.upsert / update_fields
# ---------------------------------------------------------------------------


@pytest.fixture
def containers_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield


@pytest.mark.asyncio
async def test_upsert_publishes_status_changed(containers_table):
    from core.repositories import container_repo

    with patch("core.repositories.container_repo.publish_status_changed") as mocked:
        await container_repo.upsert("owner_1", {"status": "provisioning"})
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_update_fields_publishes_status_changed(containers_table):
    from core.repositories import container_repo

    await container_repo.upsert("owner_1", {"status": "provisioning"})
    with patch("core.repositories.container_repo.publish_status_changed") as mocked:
        await container_repo.update_fields("owner_1", {"status": "running"})
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_update_fields_on_missing_row_does_not_publish(containers_table):
    """No row -> no write -> no nudge (nothing for the client to re-fetch)."""
    from core.repositories import container_repo

    with patch("core.repositories.container_repo.publish_status_changed") as mocked:
        result = await container_repo.update_fields("owner_ghost", {"status": "running"})
    assert result is None
    mocked.assert_not_called()


# ---------------------------------------------------------------------------
# billing-accounts table -- set_subscription / set_team_subscription /
# set_provider_choice
# ---------------------------------------------------------------------------


@pytest.fixture
def billing_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield resource.Table("test-billing-accounts")


@pytest.mark.asyncio
async def test_set_subscription_publishes_status_changed(billing_table):
    from core.repositories import billing_repo

    billing_table.put_item(Item={"owner_id": "owner_1", "created_at": "2026-01-01T00:00:00Z"})
    with patch("core.repositories.billing_repo.publish_status_changed") as mocked:
        result = await billing_repo.set_subscription(owner_id="owner_1", subscription_id="sub_1", status="active")
    assert result is not None
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_set_subscription_missing_row_does_not_publish(billing_table):
    from core.repositories import billing_repo

    with patch("core.repositories.billing_repo.publish_status_changed") as mocked:
        result = await billing_repo.set_subscription(owner_id="owner_ghost", subscription_id="sub_1", status="active")
    assert result is None
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_set_team_subscription_publishes_status_changed(billing_table):
    from core.repositories import billing_repo

    billing_table.put_item(Item={"owner_id": "org_1", "created_at": "2026-01-01T00:00:00Z"})
    with patch("core.repositories.billing_repo.publish_status_changed") as mocked:
        result = await billing_repo.set_team_subscription(
            "org_1",
            subscription_id="sub_1",
            subscription_item_id="si_1",
            status="active",
            seat_count=3,
        )
    assert result is not None
    mocked.assert_called_once_with("org_1")


@pytest.mark.asyncio
async def test_set_provider_choice_publishes_status_changed(billing_table):
    from core.repositories import billing_repo

    billing_table.put_item(Item={"owner_id": "owner_1", "created_at": "2026-01-01T00:00:00Z"})
    with patch("core.repositories.billing_repo.publish_status_changed") as mocked:
        await billing_repo.set_provider_choice("owner_1", provider_choice="bedrock_claude", byo_provider=None)
    mocked.assert_called_once_with("owner_1")


# ---------------------------------------------------------------------------
# paperclip-companies table -- PaperclipRepo.put / update_status /
# claim_funnel_submission
# ---------------------------------------------------------------------------


@pytest.fixture
def paperclip_repo():
    from core.repositories.paperclip_repo import PaperclipRepo

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-paperclip-companies",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield PaperclipRepo(table_name="test-paperclip-companies", region="us-east-1")


def _make_company(**overrides):
    from core.repositories.paperclip_repo import PaperclipCompany

    now = datetime.now(timezone.utc)
    fields = dict(
        user_id="owner_1",
        org_id="org_owner_1",
        company_id="co_1",
        paperclip_user_id="pc_1",
        paperclip_password_encrypted="enc",
        service_token_encrypted="enc",
        status="active",
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return PaperclipCompany(**fields)


@pytest.mark.asyncio
async def test_paperclip_put_publishes_status_changed(paperclip_repo):
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        await paperclip_repo.put(_make_company())
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_paperclip_update_status_publishes_on_success(paperclip_repo):
    await paperclip_repo.put(_make_company(status="provisioning"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        landed = await paperclip_repo.update_status("owner_1", status="active")
    assert landed is True
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_paperclip_update_status_does_not_publish_on_guard_failure(paperclip_repo):
    await paperclip_repo.put(_make_company(status="active"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        landed = await paperclip_repo.update_status("owner_1", status="failed", not_if_status="active")
    assert landed is False
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_claim_funnel_submission_publishes_on_win(paperclip_repo):
    from core.repositories.paperclip_repo import FunnelClaimResult

    await paperclip_repo.put(_make_company(onboarding_generation="01G", funnel_state="not_started"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        result = await paperclip_repo.claim_funnel_submission("owner_1", onboarding_generation="01G")
    assert result is FunnelClaimResult.WON
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_claim_funnel_submission_does_not_publish_when_already_submitted(paperclip_repo):
    from core.repositories.paperclip_repo import FunnelClaimResult

    await paperclip_repo.put(_make_company(onboarding_generation="01G", funnel_state="submitted"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        result = await paperclip_repo.claim_funnel_submission("owner_1", onboarding_generation="01G")
    assert result is FunnelClaimResult.ALREADY_SUBMITTED
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_record_onboarding_ids_publishes_status_changed(paperclip_repo):
    """The goal/project ids land AFTER claim_funnel_submission's nudge and are
    themselves composed by compute_status -- so this later write must nudge
    again, or a tab that revalidated off the early 'submitted' event sits on a
    contract missing the ids."""
    await paperclip_repo.put(_make_company(onboarding_generation="01G", funnel_state="submitted"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        await paperclip_repo.record_onboarding_ids(
            "owner_1", onboarding_generation="01G", goal_id="goal_1", project_id="proj_1"
        )
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_revert_funnel_submission_publishes_on_landed_revert(paperclip_repo):
    """A landed rollback moves funnel_state back and drops the ids -- all
    composed facts -- so the tab that saw the optimistic 'submitted' gets a
    nudge to re-fetch the reverted contract."""
    await paperclip_repo.put(_make_company(onboarding_generation="01G", funnel_state="submitted"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        await paperclip_repo.revert_funnel_submission("owner_1", onboarding_generation="01G")
    mocked.assert_called_once_with("owner_1")


@pytest.mark.asyncio
async def test_revert_funnel_submission_does_not_publish_on_guard_failure(paperclip_repo):
    """No standing claim (funnel already advanced/reverted) -> swallowed guard
    failure -> no write -> no nudge."""
    await paperclip_repo.put(_make_company(onboarding_generation="01G", funnel_state="complete"))
    with patch("core.repositories.paperclip_repo.publish_status_changed") as mocked:
        await paperclip_repo.revert_funnel_submission("owner_1", onboarding_generation="01G")
    mocked.assert_not_called()
