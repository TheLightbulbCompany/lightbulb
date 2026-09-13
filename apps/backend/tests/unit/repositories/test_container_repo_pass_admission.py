import pytest
from botocore.exceptions import ClientError
from unittest.mock import MagicMock, patch
from core.repositories import container_repo


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
    assert container_repo.PASS_KEY_PREFIX in kwargs["ExpressionAttributeValues"].values()
