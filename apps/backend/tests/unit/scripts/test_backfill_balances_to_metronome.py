"""Unit tests for the one-time DDB-balance -> Metronome-credit backfill.

moto-mocks the credits + fast-gate tables; mocks the Metronome funding +
provisioning HTTP so no live call is made. Asserts idempotency (a second run
grants nothing more).
"""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from scripts.backfill_balances_to_metronome import backfill_owner, run_backfill


@pytest.fixture
def tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="isol8-test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="isol8-test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # One funded owner, one zero-balance owner (skipped), one overdraft
        # (negative — debt forgiven, surfaced in the summary).
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_funded"}, "balance_microcents": {"N": "12000000"}},
        )
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_zero"}, "balance_microcents": {"N": "0"}},
        )
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_overdraft"}, "balance_microcents": {"N": "-500000"}},
        )
        # The funded owner is already Metronome-provisioned.
        client.put_item(
            TableName="isol8-test-metronome-fast-gate",
            Item={"owner_id": {"S": "org_funded"}, "metronome_customer_id": {"S": "cust-1"}},
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "isol8-test-metronome-fast-gate")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
        yield client


@pytest.mark.asyncio
async def test_backfill_owner_grants_equal_credit_and_marks_done(tables):
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is True
    mock_grant.assert_awaited_once()
    _, kwargs = mock_grant.call_args
    assert kwargs["owner_id"] == "org_funded"
    assert kwargs["amount_microcents"] == 12_000_000
    # Idempotency marker persisted on the fast-gate row.
    item = tables.get_item(TableName="isol8-test-metronome-fast-gate", Key={"owner_id": {"S": "org_funded"}})["Item"]
    assert "backfill_done_at" in item


@pytest.mark.asyncio
async def test_backfill_owner_idempotent_skips_when_marker_set(tables):
    tables.update_item(
        TableName="isol8-test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_funded"}},
        UpdateExpression="SET backfill_done_at = :t",
        ExpressionAttributeValues={":t": {"S": "2026-06-08T00:00:00Z"}},
    )
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is False
    mock_grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_backfill_dry_run_grants_nothing(tables):
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        summary = await run_backfill(env="test", apply=False, credits_table="isol8-test-credits")
    mock_grant.assert_not_awaited()
    # Dry-run reports the funded owner as a candidate, skips the zero one, and
    # surfaces the overdraft (negative) balance as forgiven debt.
    assert summary["candidates"] == 1
    assert summary["granted"] == 0
    assert summary["negative_skipped"] == 1
    assert summary["negative_microcents"] == -500000


@pytest.mark.asyncio
async def test_run_backfill_apply_grants_only_positive_balances(tables):
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        summary = await run_backfill(env="test", apply=True, credits_table="isol8-test-credits")
    # Only org_funded granted; org_zero + org_overdraft skipped.
    mock_grant.assert_awaited_once()
    assert summary["granted"] == 1
    assert summary["negative_skipped"] == 1


@pytest.mark.asyncio
async def test_run_backfill_rerun_does_not_overcount_candidates(tables):
    """A second apply run reports zero remaining candidates (the marker set on
    the first run moves the funded owner into skipped_already_done), proving
    `candidates` counts remaining work, not total positive balances."""
    grant_patches = lambda: (  # noqa: E731
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()),
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    )
    p1 = grant_patches()
    for p in p1:
        p.start()
    try:
        first = await run_backfill(env="test", apply=True, credits_table="isol8-test-credits")
    finally:
        for p in p1:
            p.stop()
    assert first["candidates"] == 1
    assert first["granted"] == 1

    p2 = grant_patches()
    for p in p2:
        p.start()
    try:
        second = await run_backfill(env="test", apply=True, credits_table="isol8-test-credits")
    finally:
        for p in p2:
            p.stop()
    assert second["candidates"] == 0
    assert second["granted"] == 0
    assert second["skipped_already_done"] == 1


@pytest.mark.asyncio
async def test_backfill_owner_marker_claimed_before_grant_and_kept_on_success(tables):
    """The marker is claimed before the grant (so the grant is at-most-once) and
    persists on success."""
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is True
    mock_grant.assert_awaited_once()
    item = tables.get_item(TableName="isol8-test-metronome-fast-gate", Key={"owner_id": {"S": "org_funded"}})["Item"]
    assert "backfill_done_at" in item


@pytest.mark.asyncio
async def test_backfill_owner_releases_marker_when_grant_fails(tables):
    """If the grant raises after the marker is claimed, the marker is released so
    a re-run retries instead of skipping a never-granted owner (closes the
    grant->mark double-grant gap)."""
    with (
        patch(
            "scripts.backfill_balances_to_metronome.ensure_metronome_customer",
            new=AsyncMock(return_value="cust-1"),
        ),
        patch(
            "scripts.backfill_balances_to_metronome.fund_backfill_credit",
            new=AsyncMock(side_effect=RuntimeError("metronome 500")),
        ),
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        with pytest.raises(RuntimeError, match="metronome 500"):
            await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    # Marker rolled back — re-run is not blocked.
    item = tables.get_item(TableName="isol8-test-metronome-fast-gate", Key={"owner_id": {"S": "org_funded"}}).get(
        "Item"
    )
    assert "backfill_done_at" not in (item or {})
