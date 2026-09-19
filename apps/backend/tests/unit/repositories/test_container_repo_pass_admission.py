import os

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws
from unittest.mock import MagicMock, patch
from core.repositories import container_repo

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Real (moto) conditional-write semantics -- a MagicMock can't validate the
    ConditionExpression boundary, only a real (or moto-emulated) DynamoDB can."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-containers")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


def _table(side_effect=None):
    t = MagicMock()
    t.update_item = MagicMock(side_effect=side_effect)
    return t


def _conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


@pytest.mark.asyncio
async def test_first_claimant_wins_the_window():
    with patch.object(container_repo, "_get_table", return_value=_table()):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is True


@pytest.mark.asyncio
async def test_second_claimant_in_the_same_window_loses():
    with patch.object(container_repo, "_get_table", return_value=_table(_conditional_failure())):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is False


@pytest.mark.asyncio
async def test_claim_targets_a_reserved_key_that_cannot_collide_with_an_owner():
    table = _table()
    with patch.object(container_repo, "_get_table", return_value=table):
        await container_repo.try_claim_pass("run_ledger", interval_s=60, now=1_700_000_000)
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"owner_id": "__pass__#run_ledger"}
    # cutoff = now - interval_s; the window, not process uptime, decides.
    assert kwargs["ExpressionAttributeValues"][":cutoff"] == 1_700_000_000 - 60
    assert kwargs["ExpressionAttributeValues"][":now"] == 1_700_000_000


@pytest.mark.asyncio
async def test_a_dynamodb_fault_fails_CLOSED():
    """The deliberate inverse of try_claim_daily_system_notice, which fails OPEN.
    A fleet-wide fail-open costs N x every scan exactly when DynamoDB is unhappy."""
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with patch.object(container_repo, "_get_table", return_value=_table(boom)):
        assert await container_repo.try_claim_pass("owner_day_rollup", interval_s=3600) is False


@pytest.mark.asyncio
async def test_scan_all_hides_the_reserved_pass_rows_from_fleet_consumers():
    """update_service.schedule_task_definition_refresh_for_fleet queues an update for
    EVERY row scan_all returns; a pass row must never reach it."""
    table = MagicMock()
    table.scan = MagicMock(return_value={"Items": [{"owner_id": "user_1"}]})
    with patch.object(container_repo, "_get_table", return_value=table):
        rows = await container_repo.scan_all()
    assert rows == [{"owner_id": "user_1"}]
    kwargs = table.scan.call_args.kwargs
    assert "begins_with" in kwargs["FilterExpression"]
    # ONE prefix covers every reserved row, pass admission included -- see RESERVED_KEY_PREFIX.
    assert container_repo.RESERVED_KEY_PREFIX in kwargs["ExpressionAttributeValues"].values()
    assert container_repo.PASS_KEY_PREFIX.startswith(container_repo.RESERVED_KEY_PREFIX)


@pytest.mark.asyncio
async def test_admits_at_exactly_interval_s_elapsed(dynamodb_table):
    """The boundary case a strict `<` got wrong: refused one second before the
    window closes, admitted exactly AT interval_s. A pass whose interval
    equals its caller's tick (due_updates, paperclip_retry, trial_idle,
    run_ledger -- all 60s) would otherwise only ever re-admit every OTHER
    tick."""
    t = 1_700_000_000
    assert await container_repo.try_claim_pass("run_ledger", interval_s=60, now=t) is True
    assert await container_repo.try_claim_pass("run_ledger", interval_s=60, now=t + 59) is False
    assert await container_repo.try_claim_pass("run_ledger", interval_s=60, now=t + 60) is True


@pytest.mark.asyncio
async def test_a_botocore_error_with_no_response_code_also_fails_closed():
    """NoCredentialsError (and endpoint/connect/read timeouts) carry no
    ``Error`` code, so an `except ClientError` alone lets them escape and abort
    the tick's remaining passes. The criterion is ANY failure -> not admitted."""
    with patch.object(container_repo, "_get_table", return_value=_table(NoCredentialsError())):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is False


@pytest.mark.asyncio
async def test_a_persistent_fault_emits_its_own_outcome_fault_metric():
    """The callers' own admitted() wrapper only ever emits won/lost from the
    boolean, so a throttle (or any other real DynamoDB error) is invisible to
    it -- indistinguishable from ordinary contention. try_claim_pass must
    surface it directly."""
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with (
        patch.object(container_repo, "_get_table", return_value=_table(boom)),
        patch.object(container_repo, "put_metric") as mock_metric,
    ):
        assert await container_repo.try_claim_pass("owner_day_rollup", interval_s=3600) is False
    mock_metric.assert_called_once_with(
        "lifecycle.pass.claimed", dimensions={"pass": "owner_day_rollup", "outcome": "fault"}
    )


@pytest.mark.asyncio
async def test_a_botocore_error_also_emits_outcome_fault():
    with (
        patch.object(container_repo, "_get_table", return_value=_table(NoCredentialsError())),
        patch.object(container_repo, "put_metric") as mock_metric,
    ):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is False
    mock_metric.assert_called_once_with(
        "lifecycle.pass.claimed", dimensions={"pass": "paperclip_purge", "outcome": "fault"}
    )


@pytest.mark.asyncio
async def test_ordinary_contention_emits_no_fault_metric():
    """ConditionalCheckFailedException is expected contention, not a fault --
    the caller's own metric already reports it as "lost" (see
    test_pass_admission_wiring.test_admission_emits_won_and_lost); try_claim_pass
    must not double-report it as a fault."""
    with (
        patch.object(container_repo, "_get_table", return_value=_table(_conditional_failure())),
        patch.object(container_repo, "put_metric") as mock_metric,
    ):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is False
    mock_metric.assert_not_called()
