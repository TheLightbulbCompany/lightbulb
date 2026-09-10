import httpx
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


@pytest.mark.asyncio
async def test_plugin_ingress_timeout_is_a_short_retry_and_clears_the_active_run(monkeypatch):
    """A slow plugin POST (prod: ReadTimeout on a long founder turn) must not
    hold the FIFO group for 300s — the plugin dedupes by eventId, so retry soon."""
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    cleared = []

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

        def mark_run_active(self, *_args, **_kwargs):
            return None

        def clear_active_run(self, owner_id, session_key):
            cleared.append((owner_id, session_key))

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return {"gateway_token": "gw"}, "10.0.0.9"

    async def fake_post(_url, _body, _token):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr("core.services.openclaw_work.get_ecs_manager", lambda: FakeManager())
    monkeypatch.setattr("core.services.openclaw_work.get_gateway_pool", lambda: FakePool())
    monkeypatch.setattr(worker, "_post", fake_post)

    async def fake_list_members(_room_id):
        return [{"kind": "agent", "id": "a1", "displayName": "A", "mentionHandle": "a", "listenMode": "active"}]

    async def fake_get_member(_room_id, _kind, _member_id):
        return {"kind": "agent", "id": "a1"}

    async def fake_body(**_kwargs):
        return {"version": 1}

    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_members", fake_list_members)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.get_member", fake_get_member)
    monkeypatch.setattr("core.services.openclaw_work.build_ingress_body", fake_body)

    meta = {"ownerId": "owner-1", "roomId": "11111111-1111-4111-8111-111111111111", "kind": "dm", "revision": 1}
    member = {"kind": "agent", "id": "a1"}
    message = {"messageId": "m1", "messageSeq": 1, "author": {"kind": "human", "id": "u1"}}

    with pytest.raises(WorkRetry, match="plugin ingress ReadTimeout"):
        await worker._dispatch_message(meta, member, message, "user_request")
    assert cleared == [("owner-1", "agent:a1:lightbulb:channel:11111111-1111-4111-8111-111111111111")]
