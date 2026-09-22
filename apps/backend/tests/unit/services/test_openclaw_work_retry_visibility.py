from unittest.mock import AsyncMock

import httpx
import pytest

from core.config import settings
from core.services.openclaw_work import (
    _RETRY_VISIBILITY_SECONDS,
    OpenClawWorkWorker,
    WorkRetry,
    build_ingress_body,
)

_ROOM = "11111111-1111-4111-8111-111111111111"
WS = "44444444-4444-4444-8444-444444444444"
KEY = "55555555-5555-4555-8555-555555555555"


@pytest.mark.asyncio
async def test_canonical_ingress_mints_optional_room_grant_after_membership_check(monkeypatch):
    from core.services import local_session_grants
    from core.services import openclaw_work as work

    room = _ROOM
    event = "22222222-2222-4222-8222-222222222222"
    meta = {"ownerId": "owner_test", "roomId": room, "workspaceId": WS, "deliveryKey": KEY}
    member = {"id": "friday"}
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "dummy", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    membership = AsyncMock(return_value=member)
    monkeypatch.setattr(work.messaging_repo, "get_member", membership)
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(side_effect=lambda **kw: {"eventId": event}))
    monkeypatch.setattr(work.messaging_repo, "create_run", AsyncMock(return_value=True))
    monkeypatch.setattr(work.messaging_repo, "end_run", AsyncMock(return_value={"runId": "x"}))
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    post = AsyncMock(return_value=httpx.Response(202))
    monkeypatch.setattr(worker, "_post", post)
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    body = post.call_args.args[2]
    assert "invocation" not in body
    caller = local_session_grants.verify(post.call_args.args[4])
    assert (caller.owner_id, caller.room_id, caller.agent_id) == ("owner_test", room, "friday")
    membership.return_value = None
    post.reset_mock()
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    post.assert_not_called()
    membership.return_value = member
    monkeypatch.delenv("PAPERCLIP_SERVICE_TOKEN_KEY")
    await worker._dispatch_message(meta, member, {"messageId": event}, "user_request")
    assert post.call_args.args[4] is None


class FakeSqs:
    def __init__(self):
        self.visibility_calls = []

    def change_message_visibility(self, **kwargs):
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs):
        pass


@pytest.mark.asyncio
async def test_invocation_header_keeps_canonical_body_and_gateway_authorization_unchanged(monkeypatch):
    """The invocation grant rides its own header, built once in `_post` before the
    transport branch -- so this holds for AgentCore too. `host_for` is pinned to
    "agentcore" fleet-wide (deletion PR-1), so the raw ECS httpx POST this test used to
    drive (a `{}` container with no `runtime_host`) is dead code now; this drives the
    same assertions through the surviving `runtime_transport.request` seam instead."""
    import json
    from core.services import local_session_grants
    from core.services import openclaw_work as work
    from core.services import runtime_transport

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    grant = local_session_grants.mint(
        "owner_test", "11111111-1111-4111-8111-111111111111", "friday", "22222222-2222-4222-8222-222222222222"
    )
    body = {"version": 1, "eventId": "canonical"}
    captured = {}

    async def fake_request(
        container, path, *, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["json_body"] = json_body
        captured["headers"] = headers
        return runtime_transport.RuntimeResponse(202, "{}", {})

    monkeypatch.setattr(work.runtime_transport, "request", fake_request)

    agentcore_container = {"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    await worker._post(
        agentcore_container,
        "/plugins/lightbulb-channel/inbound/friday",
        body,
        "gateway-dummy",
        grant,
    )

    assert captured["json_body"] == body
    assert grant not in json.dumps(captured["json_body"])
    assert captured["headers"]["Authorization"] == "Bearer gateway-dummy"
    assert captured["headers"]["X-Lightbulb-Local-Session-Token"] == grant


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

    async def fake_post(_container, _path, _body, _token, _invocation=None):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(
        "core.services.openclaw_work.container_repo.get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(worker, "_post", fake_post)

    async def fake_list_members(_room_id):
        return [{"kind": "agent", "id": "a1", "displayName": "A", "mentionHandle": "a", "listenMode": "active"}]

    async def fake_get_member(_room_id, _kind, _member_id):
        return {"kind": "agent", "id": "a1"}

    async def fake_body(**_kwargs):
        return {"version": 1}

    async def fake_create_run(*_args, **_kwargs):
        return True

    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_members", fake_list_members)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.get_member", fake_get_member)
    monkeypatch.setattr("core.services.openclaw_work.build_ingress_body", fake_body)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.create_run", fake_create_run)

    meta = {"ownerId": "owner-1", "roomId": _ROOM, "kind": "dm", "revision": 1, "workspaceId": WS, "deliveryKey": KEY}
    member = {"kind": "agent", "id": "a1"}
    message = {
        "messageId": "m1",
        "messageSeq": 1,
        "threadRootMessageId": "thread-1",
        "author": {"kind": "human", "id": "u1"},
    }

    with pytest.raises(WorkRetry, match="plugin ingress ReadTimeout"):
        await worker._dispatch_message(meta, member, message, "user_request")


@pytest.mark.asyncio
async def test_plugin_not_admitted_is_consumed_without_retry(monkeypatch):
    """A native channel admission refusal is final for this room message.

    Retrying it after the pair-loop cooldown turns suppression into a pause and
    can keep the agent-to-agent causal chain alive indefinitely.
    """
    from core.repositories import messaging_repo as repo

    from unittest.mock import AsyncMock

    monkeypatch.setattr("core.services.openclaw_work.listening_available", AsyncMock(return_value=True))
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    ended = []
    metrics = []

    class RefusedResponse:
        status_code = 409

    async def fake_post(_container, _path, _body, _token, _invocation=None):
        return RefusedResponse()

    async def fake_list_members(_room_id):
        return [{"kind": "agent", "id": "a1", "displayName": "A", "mentionHandle": "a", "listenMode": "active"}]

    async def fake_get_member(_room_id, _kind, _member_id):
        return {"kind": "agent", "id": "a1"}

    async def fake_body(**_kwargs):
        return {"version": 1}

    async def fake_create_run(*_args, **_kwargs):
        return True

    async def fake_end_run(owner_id, run_id, **fields):
        ended.append((owner_id, run_id, fields))
        return {"runId": run_id}

    monkeypatch.setattr(
        "core.services.openclaw_work.container_repo.get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.list_members", fake_list_members)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.get_member", fake_get_member)
    monkeypatch.setattr("core.services.openclaw_work.build_ingress_body", fake_body)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.create_run", fake_create_run)
    monkeypatch.setattr("core.services.openclaw_work.messaging_repo.end_run", fake_end_run)
    monkeypatch.setattr("core.services.openclaw_work.put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    monkeypatch.setattr(worker, "_post", fake_post)

    meta = {
        "ownerId": "owner-1",
        "roomId": _ROOM,
        "kind": "channel",
        "revision": 1,
        "workspaceId": WS,
        "deliveryKey": KEY,
    }
    member = {"kind": "agent", "id": "a1"}
    message = {
        "messageId": "m1",
        "messageSeq": 1,
        "threadRootMessageId": None,
        "author": {"kind": "agent", "id": "a2"},
    }

    await worker._dispatch_message(meta, member, message, "room_event")

    run_id = repo.run_id_for("a1", "m1")
    assert ended == [("owner-1", run_id, {"outcome": "not_admitted"})]
    assert [(name, kwargs["dimensions"]) for name, kwargs in metrics] == [
        ("messaging.jev.route", {"outcome": "fallback"}),  # Jev is unconfigured in tests
        ("messaging.run.dispatched", {"outcome": "new", "event_kind": "room_event"}),
        ("messaging.ingress.post", {"status_class": "409", "event_kind": "room_event"}),
        ("messaging.ingress.latency", {"event_kind": "room_event"}),
        ("messaging.ambient_gate", {"outcome": "not_admitted", "event_kind": "room_event"}),
    ]


@pytest.mark.asyncio
async def test_ingress_history_reads_the_last_fifty_messages(monkeypatch):
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

    assert seen_limits == [50]


@pytest.mark.asyncio
async def test_agentcore_ingress_budget_outlasts_a_cold_microvm_start(monkeypatch):
    """The ingress POST is the call that wakes a parked AgentCore workspace: AgentCore holds
    the invoke through the microVM start and the gateway boot (54-57s measured), so a 30s
    budget would abandon every cold start and the room turn would never be accepted."""
    from unittest.mock import AsyncMock

    from core.services import openclaw_work as work
    from core.services import runtime_transport

    seen = AsyncMock(return_value=runtime_transport.RuntimeResponse(202, '{"status":"accepted"}'))
    monkeypatch.setattr(work.runtime_transport, "request", seen)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    container = {"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x", "owner_id": "o"}
    res = await worker._post(container, "/plugins/lightbulb-channel/inbound/a", {"x": 1}, "tok")
    assert res.status_code == 202
    kwargs = seen.call_args.kwargs
    assert kwargs["timeout_s"] >= 60.0
    assert kwargs["json_body"] == {"x": 1}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_every_503_retries_at_the_cold_start_pace(monkeypatch):
    """Task 8a: a provider switch no longer refuses turns, so there is no switch timescale --
    even an N-1 bridge's own freeze refusal retries like any other 503."""
    from core.services import openclaw_work as work

    meta = {"ownerId": "owner_test", "roomId": _ROOM, "workspaceId": WS, "deliveryKey": KEY}
    member = {"id": "friday"}
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "dummy", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=member))
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(side_effect=lambda **kw: {}))
    monkeypatch.setattr(work.messaging_repo, "create_run", AsyncMock(return_value=True))
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    for refusal in (httpx.Response(503, json={"error": "provider_switch_frozen"}), httpx.Response(503, text="busy")):
        monkeypatch.setattr(worker, "_post", AsyncMock(return_value=refusal))
        with pytest.raises(WorkRetry) as caught:
            await worker._dispatch_message(meta, member, {"messageId": "m1"}, "user_request")
        assert caught.value.visibility is None
