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
