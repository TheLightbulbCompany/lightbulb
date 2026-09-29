import pytest

from scripts import work_lambda


def _record() -> dict:
    return {
        "messageId": "m1",
        "receiptHandle": "rh-m1",
        "body": "{}",
        "attributes": {"MessageGroupId": "room-agent", "SentTimestamp": "1"},
    }


class FakeWorker:
    def __init__(self, completed: bool):
        self.completed = completed
        self.messages = []

    async def _process_message(self, message: dict) -> bool:
        self.messages.append(message)
        return self.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_one_record_reports_only_failed_work(completed):
    worker = FakeWorker(completed)
    result = await work_lambda.process_batch(worker, [_record()])
    assert result == {"batchItemFailures": [] if completed else [{"itemIdentifier": "m1"}]}
    assert worker.messages == [
        {
            "MessageId": "m1",
            "ReceiptHandle": "rh-m1",
            "Body": "{}",
            "Attributes": {"MessageGroupId": "room-agent", "SentTimestamp": "1"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [[], [_record(), _record()], [{**_record(), "attributes": {}}]])
async def test_unexpected_batch_or_missing_fifo_group_fails_closed(records):
    worker = FakeWorker(True)
    with pytest.raises(ValueError):
        await work_lambda.process_batch(worker, records)
    assert worker.messages == []
