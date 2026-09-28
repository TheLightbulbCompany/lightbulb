import pytest

from scripts import work_lambda


def _record(mid: str, group: str) -> dict:
    return {
        "messageId": mid,
        "receiptHandle": f"rh-{mid}",
        "body": "{}",
        "attributes": {"MessageGroupId": group, "SentTimestamp": "1"},
    }


class FakeWorker:
    def __init__(self, failing: set[str]):
        self.failing = failing
        self.processed: list[str] = []
        self.released: list[str] = []

    async def _process_message(self, message: dict) -> bool:
        self.processed.append(message["MessageId"])
        return message["MessageId"] not in self.failing

    async def _return_visibility(self, message: dict, timeout: int = 0) -> None:
        self.released.append(message["ReceiptHandle"])


@pytest.mark.asyncio
async def test_a_failure_fails_the_rest_of_its_group_only():
    worker = FakeWorker(failing={"a2"})
    records = [_record("a1", "A"), _record("b1", "B"), _record("a2", "A"), _record("a3", "A"), _record("b2", "B")]

    result = await work_lambda.process_batch(worker, records)

    assert result == {"batchItemFailures": [{"itemIdentifier": "a2"}, {"itemIdentifier": "a3"}]}
    # a3 is never run out of order; it is released for redelivery behind a2.
    assert "a3" not in worker.processed
    assert worker.released == ["rh-a3"]
    assert set(worker.processed) == {"a1", "a2", "b1", "b2"}


@pytest.mark.asyncio
async def test_all_ok_reports_no_failures_and_passes_the_receive_shape():
    worker = FakeWorker(failing=set())

    result = await work_lambda.process_batch(worker, [_record("m1", "G")])

    assert result == {"batchItemFailures": []}
    assert work_lambda._as_sqs_message(_record("m1", "G")) == {
        "MessageId": "m1",
        "ReceiptHandle": "rh-m1",
        "Body": "{}",
        "Attributes": {"MessageGroupId": "G", "SentTimestamp": "1"},
    }
