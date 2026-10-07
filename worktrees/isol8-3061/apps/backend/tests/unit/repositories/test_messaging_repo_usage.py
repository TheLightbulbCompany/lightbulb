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
async def test_decision_claim_is_put_if_absent_or_stale_and_result_overwrites():
    with (
        patch.object(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging"),
        patch("core.repositories.messaging_repo.run_in_thread", new_callable=AsyncMock) as run,
        patch("core.repositories.messaging_repo.time.time", return_value=1000.0),
    ):
        assert await messaging_repo.claim_decision("room-1", "m1") is True
        kwargs = run.await_args.kwargs
        item = kwargs["Item"]
        # Not WORK#: the EventBridge Pipe filters on that prefix and must never see this row.
        assert (item["pk"], item["sk"]) == ("ROOM#room-1", "DECISION#m1") and "expiresAt" in item
        assert item["decision"] == {"pending": True, "claimedAt": 1000, "attempts": 0}
        # Absent, a rate-limit retry whose backoff elapsed, OR a pending claim older than 60 s
        # (the winner died) may be taken over.
        assert kwargs["ConditionExpression"] == (
            "attribute_not_exists(sk) OR (#d.#r = :t AND #d.#ra <= :now) OR (#d.#p = :t AND #d.#c < :stale)"
        )
        assert kwargs["ExpressionAttributeValues"] == {":t": True, ":now": 1000, ":stale": 940}
        run.side_effect = _conditional_failure
        assert await messaging_repo.claim_decision("room-1", "m1") is False

        run.side_effect = None
        final = await messaging_repo.finalize_decision("room-1", "m1", {"who": "a1", "whoP": {"a1": 0.9}})
        kwargs = run.await_args.kwargs
        # First final write wins: only a still-pending (or absent) row may be finalized, so a late
        # winner and a timed-out loser can never act on two different decisions.
        assert kwargs["ConditionExpression"] == "attribute_not_exists(sk) OR #d.#p = :t OR #d.#r = :t"
        assert str(kwargs["Item"]["decision"]["whoP"]["a1"]) == "0.9"  # float -> Decimal for DynamoDB
        assert final == {"who": "a1", "whoP": {"a1": 0.9}}
        # Lost the race: return what is stored, and the caller follows it.
        calls = iter([_conditional_failure, lambda *a, **k: {"Item": {"decision": {"fallback": True}}}])
        run.side_effect = lambda *a, **k: next(calls)(*a, **k)
        assert await messaging_repo.finalize_decision("room-1", "m1", {"who": "a2"}) == {"fallback": True}
        run.side_effect = None

        run.return_value = {"Item": {"pk": "ROOM#room-1", "sk": "DECISION#m1", "decision": {"who": "a1"}}}
        assert await messaging_repo.get_decision("room-1", "m1") == {"who": "a1"}
        run.return_value = {}
        assert await messaging_repo.get_decision("room-1", "m1") is None
