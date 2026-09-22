from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from core.repositories import messaging_repo


def _conditional_failure(*args, **kwargs):
    raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


@pytest.mark.asyncio
async def test_claim_usage_receipt_is_first_writer_wins():
    with (
        patch.object(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging"),
        patch("core.repositories.messaging_repo.run_in_thread", new_callable=AsyncMock) as run,
    ):
        assert await messaging_repo.claim_usage_receipt("org_1", "run-1") is True
        item = run.await_args.kwargs["Item"]
        assert (item["pk"], item["sk"]) == ("OWNER#org_1", "USAGE#run-1") and "expiresAt" in item
        run.side_effect = _conditional_failure
        assert await messaging_repo.claim_usage_receipt("org_1", "run-1") is False


@pytest.mark.asyncio
async def test_decision_row_is_put_once_and_read_back():
    with (
        patch.object(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging"),
        patch("core.repositories.messaging_repo.run_in_thread", new_callable=AsyncMock) as run,
    ):
        assert await messaging_repo.put_decision_if_absent("room-1", "m1", {"who": "a1", "whoP": {"a1": 0.9}}) is True
        kwargs = run.await_args.kwargs
        item = kwargs["Item"]
        # Not WORK#: the EventBridge Pipe filters on that prefix and must never see this row.
        assert (item["pk"], item["sk"]) == ("ROOM#room-1", "DECISION#m1") and "expiresAt" in item
        assert kwargs["ConditionExpression"] == "attribute_not_exists(sk)"
        assert str(item["decision"]["whoP"]["a1"]) == "0.9"  # float -> Decimal for DynamoDB
        run.side_effect = _conditional_failure
        assert await messaging_repo.put_decision_if_absent("room-1", "m1", {"who": "a2"}) is False

        run.side_effect = None
        run.return_value = {"Item": {"pk": "ROOM#room-1", "sk": "DECISION#m1", "decision": {"who": "a1"}}}
        assert await messaging_repo.get_decision("room-1", "m1") == {"who": "a1"}
        run.return_value = {}
        assert await messaging_repo.get_decision("room-1", "m1") is None
