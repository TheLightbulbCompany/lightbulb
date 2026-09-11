import httpx
import pytest

from core.config import settings
from core.services.openclaw_work import _RETRY_VISIBILITY_SECONDS, OpenClawWorkWorker, WorkRetry


@pytest.mark.asyncio
async def test_canonical_ingress_mints_optional_room_grant_after_membership_check(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from core.services import local_session_grants
    from core.services import openclaw_work as work

    room = "11111111-1111-4111-8111-111111111111"
    event = "22222222-2222-4222-8222-222222222222"
    meta, member = {"ownerId": "owner_test", "roomId": room}, {"id": "friday"}
    pool = Mock(ensure_connection=AsyncMock(), subscribe_room_session=AsyncMock())
    monkeypatch.setattr(work, "get_gateway_pool", lambda: pool)
    manager = Mock(resolve_running_container=AsyncMock(return_value=({"gateway_token": "dummy"}, "127.0.0.1")))
    monkeypatch.setattr(work, "get_ecs_manager", lambda: manager)
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    membership = AsyncMock(return_value=member)
    monkeypatch.setattr(work.messaging_repo, "get_member", membership)
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(side_effect=lambda **kw: {"eventId": event}))
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    post = AsyncMock(return_value=httpx.Response(202))
    monkeypatch.setattr(worker, "_post", post)
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    body = post.call_args.args[1]
    assert "invocation" not in body
    caller = local_session_grants.verify(post.call_args.args[3])
    assert (caller.owner_id, caller.room_id, caller.agent_id) == ("owner_test", room, "friday")
    membership.return_value = None
    post.reset_mock()
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    post.assert_not_called()
    membership.return_value = member
    monkeypatch.delenv("PAPERCLIP_SERVICE_TOKEN_KEY")
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    assert post.call_args.args[3] is None


class FakeSqs:
    def __init__(self):
        self.visibility_calls = []

    def change_message_visibility(self, **kwargs):
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs):
        pass


@pytest.mark.asyncio
async def test_invocation_header_keeps_canonical_body_and_gateway_authorization_unchanged(monkeypatch):
    import json
    from core.services import local_session_grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    grant = local_session_grants.mint(
        "owner_test", "11111111-1111-4111-8111-111111111111", "friday", "22222222-2222-4222-8222-222222222222"
    )
    body = {"version": 1, "eventId": "canonical"}

    async def receive(request):
        assert json.loads(request.content) == body
        assert grant not in request.content.decode()
        assert request.headers["Authorization"] == "Bearer gateway-dummy"
        assert request.headers["X-Lightbulb-Local-Session-Token"] == grant
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        worker = OpenClawWorkWorker(sqs_client=FakeSqs(), http_client=client)
        await worker._post(
            "http://127.0.0.1:18789/plugins/lightbulb-channel/inbound/friday", body, "gateway-dummy", grant
        )


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

    async def fake_post(_url, _body, _token, _invocation):
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
