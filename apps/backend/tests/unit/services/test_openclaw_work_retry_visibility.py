import httpx
import pytest

from core.config import settings
from core.services.openclaw_work import (
    _RETRY_VISIBILITY_SECONDS,
    OpenClawWorkWorker,
    WorkRetry,
    build_ingress_body,
)


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
    subscribed = []
    marked = []

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

        async def subscribe_room_session(self, owner_id, session_key):
            subscribed.append((owner_id, session_key))

        def mark_run_active(self, *args, **kwargs):
            marked.append((args, kwargs))

        def clear_active_run(self, owner_id, session_key, **kwargs):
            cleared.append((owner_id, session_key, kwargs))

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
    message = {
        "messageId": "m1",
        "messageSeq": 1,
        "threadRootMessageId": "thread-1",
        "author": {"kind": "human", "id": "u1"},
    }

    with pytest.raises(WorkRetry, match="plugin ingress ReadTimeout"):
        await worker._dispatch_message(meta, member, message, "user_request")
    session_key = "agent:a1:lightbulb:channel:11111111-1111-4111-8111-111111111111"
    assert cleared == [("owner-1", session_key, {"preserve_room_context": True})]
    # Channel-initiated runs are hidden from the Control UI upstream, so the
    # gateway streams their agent/chat events only to a session-message
    # subscriber. Dispatching without this leaves the room with zero run frames.
    assert subscribed == [("owner-1", session_key)]
    assert marked[0][1]["thread_root_message_id"] == "thread-1"


@pytest.mark.asyncio
async def test_plugin_not_admitted_is_consumed_without_retry(monkeypatch):
    """A native channel admission refusal is final for this room message.

    Retrying it after the pair-loop cooldown turns suppression into a pause and
    can keep the agent-to-agent causal chain alive indefinitely.
    """
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    cleared = []
    metrics = []

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

        async def subscribe_room_session(self, *_args):
            return None

        def mark_run_active(self, *_args, **_kwargs):
            return None

        def clear_active_run(self, owner_id, session_key, **kwargs):
            cleared.append((owner_id, session_key, kwargs))

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return {"gateway_token": "gw"}, "10.0.0.9"

    class RefusedResponse:
        status_code = 409

    async def fake_post(_url, _body, _token):
        return RefusedResponse()

    async def fake_list_members(_room_id):
        return [{"kind": "agent", "id": "a1", "displayName": "A", "mentionHandle": "a", "listenMode": "active"}]

    async def fake_get_member(_room_id, _kind, _member_id):
        return {"kind": "agent", "id": "a1"}

    async def fake_body(**_kwargs):
        return {"version": 1}

    monkeypatch.setattr("core.services.openclaw_work.get_ecs_manager", lambda: FakeManager())
    monkeypatch.setattr("core.services.openclaw_work.get_gateway_pool", lambda: FakePool())
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_members", fake_list_members)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.get_member", fake_get_member)
    monkeypatch.setattr("core.services.openclaw_work.build_ingress_body", fake_body)
    monkeypatch.setattr("core.services.openclaw_work.put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    monkeypatch.setattr(worker, "_post", fake_post)

    meta = {"ownerId": "owner-1", "roomId": "11111111-1111-4111-8111-111111111111", "kind": "channel", "revision": 1}
    member = {"kind": "agent", "id": "a1"}
    message = {
        "messageId": "m1",
        "messageSeq": 1,
        "threadRootMessageId": None,
        "author": {"kind": "agent", "id": "a2"},
    }

    await worker._dispatch_message(meta, member, message, "room_event")

    session_key = "agent:a1:lightbulb:channel:11111111-1111-4111-8111-111111111111"
    assert cleared == [("owner-1", session_key, {})]
    assert metrics == [("messaging.ambient_gate", {"dimensions": {"outcome": "not_admitted"}})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_kind", "outcome"),
    [("room_event", "ignored"), ("room_event", "replayed"), ("user_request", "replayed")],
)
async def test_consumed_outcome_clears_synthetic_run(monkeypatch, event_kind, outcome):
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    metrics = []
    cleared = []

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

        async def subscribe_room_session(self, *_args):
            return None

        def mark_run_active(self, *_args, **_kwargs):
            return None

        def clear_active_run(self, *args, **kwargs):
            cleared.append((args, kwargs))

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return {"gateway_token": "gw"}, "10.0.0.9"

    class ConsumedResponse:
        status_code = 202

        def json(self):
            return {"eventId": "m1", "status": outcome}

    async def fake_get_member(_room_id, _kind, _member_id):
        return {"kind": "agent", "id": "a1"}

    async def fake_list_members(_room_id):
        return []

    async def fake_body(**_kwargs):
        return {"version": 1}

    async def fake_post(*_args):
        return ConsumedResponse()

    monkeypatch.setattr("core.services.openclaw_work.get_ecs_manager", lambda: FakeManager())
    monkeypatch.setattr("core.services.openclaw_work.get_gateway_pool", lambda: FakePool())
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_members", fake_list_members)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.get_member", fake_get_member)
    monkeypatch.setattr("core.services.openclaw_work.build_ingress_body", fake_body)
    monkeypatch.setattr("core.services.openclaw_work.put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    monkeypatch.setattr(worker, "_post", fake_post)

    await worker._dispatch_message(
        {"ownerId": "owner-1", "roomId": "11111111-1111-4111-8111-111111111111", "kind": "channel", "revision": 1},
        {"kind": "agent", "id": "a1"},
        {"messageId": "m1", "messageSeq": 1, "author": {"kind": "human", "id": "u1"}},
        event_kind,
    )

    assert metrics == (
        [("messaging.ambient_gate", {"dimensions": {"outcome": outcome}})] if event_kind == "room_event" else []
    )
    assert len(cleared) == 1


@pytest.mark.asyncio
async def test_ingress_history_reads_only_the_twenty_messages_openclaw_renders(monkeypatch):
    seen_limits = []

    async def fake_history(_room_id, _before_seq, limit):
        seen_limits.append(limit)
        return []

    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_messages_before", fake_history)
    await build_ingress_body(
        meta={"roomId": "11111111-1111-4111-8111-111111111111", "name": "launch", "kind": "channel", "revision": 1},
        target_agent_id="22222222-2222-4222-8222-222222222222",
        event_kind="room_event",
        message={
            "messageId": "33333333-3333-4333-8333-333333333333",
            "roomId": "11111111-1111-4111-8111-111111111111",
            "messageSeq": 21,
            "roomRevision": 1,
            "author": {"kind": "human", "id": "u1", "displayName": "Founder"},
            "text": "status update",
            "mentions": [],
            "replyToMessageId": None,
            "threadRootMessageId": None,
            "createdAt": "2026-09-11T00:00:00Z",
            "runId": None,
            "attachments": [],
        },
        members=[],
    )

    assert seen_limits == [20]
