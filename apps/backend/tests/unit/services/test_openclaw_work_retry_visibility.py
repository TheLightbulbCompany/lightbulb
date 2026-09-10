import pytest

from core.config import settings
from core.services.openclaw_work import _RETRY_VISIBILITY_SECONDS, OpenClawWorkWorker, WorkRetry


class FakeSqs:
    def __init__(self):
        self.visibility_calls = []

    def change_message_visibility(self, **kwargs):
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs):
        pass


@pytest.mark.asyncio
async def test_work_retry_shortens_visibility_instead_of_holding_the_full_timeout(monkeypatch):
    """A WorkRetry (waking container, lease race, ...) must release the FIFO
    message quickly so the rest of the group doesn't queue behind a 300s hold."""
    fake_sqs = FakeSqs()
    worker = OpenClawWorkWorker(sqs_client=fake_sqs)

    async def fake_room_dispatch(_pointer):
        raise WorkRetry("runtime is waking")

    monkeypatch.setattr(worker, "_room_dispatch", fake_room_dispatch)
    monkeypatch.setattr(
        "core.services.openclaw_work.deserialize_pointer",
        lambda _body: {"kind": "room_dispatch"},
    )

    await worker._process_message({"Body": "{}", "ReceiptHandle": "rh-1"})

    assert fake_sqs.visibility_calls == [
        {
            "QueueUrl": settings.OPENCLAW_WORK_QUEUE_URL,
            "ReceiptHandle": "rh-1",
            "VisibilityTimeout": _RETRY_VISIBILITY_SECONDS,
        }
    ]
