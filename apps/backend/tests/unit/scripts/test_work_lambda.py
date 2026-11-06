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


def test_a_provisioned_environment_sets_up_at_init(monkeypatch):
    """Walk 14: a pre-warmed consumer pays the env rebuild and backend import before the first message."""
    import importlib

    from scripts import provision_lambda

    calls = []
    monkeypatch.setattr(
        provision_lambda, "load_environment", lambda: calls.append(1) or (_ for _ in ()).throw(KeyError)
    )
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    with pytest.raises(KeyError):
        importlib.reload(work_lambda)
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")
    importlib.reload(work_lambda)  # on-demand stays lazy
    assert calls == [1]
