"""The three ways the room consumer delivered wrong: a redelivery re-ran a finished turn, one
undeliverable message deafened an agent in a room forever, and SQS handed a message to a second
consumer while the first was still invoking."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import openclaw_work as work
from core.services.openclaw_work import (
    _AGENTCORE_INGRESS_TIMEOUT_S,
    _VISIBILITY_SECONDS,
    OpenClawWorkWorker,
)

ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
META = {
    "ownerId": "owner_test",
    "roomId": ROOM,
    "workspaceId": "44444444-4444-4444-8444-444444444444",
    "deliveryKey": "55555555-5555-4555-8555-555555555555",
    "kind": "dm",
}
MEMBER = {"id": AGENT, "displayName": "Friday"}


class FakeSqs:
    def __init__(self):
        self.visibility_calls = []

    def change_message_visibility(self, **kwargs):
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs):
        pass


def _wire_dispatch(monkeypatch, *, created: bool, redispatched):
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(return_value={}))
    monkeypatch.setattr(work.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(work.messaging_repo, "create_run", AsyncMock(return_value=created))
    monkeypatch.setattr(work.messaging_repo, "redispatch_run", AsyncMock(return_value=redispatched))
    monkeypatch.setattr(work.messaging_repo, "get_run", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created", "redispatched", "invoked"),
    [
        (True, None, True),  # first delivery
        (False, {"status": "dispatched"}, True),  # runtime never acknowledged it: try again
        (False, None, False),  # started, ended or stopped: never run the turn twice
    ],
)
async def test_a_redelivery_invokes_only_a_run_the_runtime_never_took(monkeypatch, created, redispatched, invoked):
    _wire_dispatch(monkeypatch, created=created, redispatched=redispatched)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    post = AsyncMock(return_value=httpx.Response(202, json={"status": "accepted"}))
    monkeypatch.setattr(worker, "_post", post)

    await worker._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request")

    assert post.called is invoked


@pytest.mark.asyncio
async def test_a_started_run_keeps_its_delivery_receipt_after_worker_redelivery(monkeypatch):
    _wire_dispatch(monkeypatch, created=False, redispatched=None)
    work.messaging_repo.get_run.return_value = {"status": "ended", "startedAt": "2026-09-29T20:00:00Z"}
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    post = AsyncMock()
    monkeypatch.setattr(worker, "_post", post)

    assert await worker._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request") is True
    post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["body", "post"])
@pytest.mark.parametrize("follow_up", [None, {"id": "f1", "reason": "done", "text": "finished"}])
async def test_oversize_closes_a_visible_failure_for_messages_and_followups(monkeypatch, stage, follow_up):
    _wire_dispatch(monkeypatch, created=True, redispatched=None)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    post = AsyncMock(return_value=httpx.Response(202))
    close = AsyncMock()
    monkeypatch.setattr(worker, "_post", post)
    monkeypatch.setattr(work, "_close_refused_run", close)
    if stage == "body":
        monkeypatch.setattr(work, "build_ingress_body", AsyncMock(return_value={"_oversized": True}))
    else:
        post.side_effect = work.IngressTooLarge("ingress exceeds 1 MiB")

    await worker._dispatch_message(
        META, MEMBER, {"messageId": "m1", "messageSeq": 1}, "user_request", follow_up=follow_up
    )

    key = "m1" if follow_up is None else "m1:followup:f1"
    close.assert_awaited_once_with("owner_test", work.messaging_repo.run_id_for(AGENT, key), 413, "user_request")
    if stage == "body":
        post.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_runtime_response_does_not_close_the_run(monkeypatch):
    _wire_dispatch(monkeypatch, created=True, redispatched=None)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_post", AsyncMock(side_effect=ValueError("invalid gateway status")))
    close = AsyncMock()
    monkeypatch.setattr(work, "_close_refused_run", close)

    with pytest.raises(ValueError, match="invalid gateway status"):
        await worker._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request")
    close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("redispatched", [None, {"status": "dispatched"}])
async def test_oversize_redelivery_closes_only_a_still_dispatched_run(monkeypatch, redispatched):
    _wire_dispatch(monkeypatch, created=False, redispatched=redispatched)
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(return_value={"_oversized": True}))
    close = AsyncMock()
    monkeypatch.setattr(work, "_close_refused_run", close)
    await OpenClawWorkWorker(sqs_client=FakeSqs())._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request")
    assert close.await_count == int(redispatched is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_room_cursor_advances_without_claiming_a_refused_turn_was_delivered(monkeypatch, accepted):
    message = {"messageId": "m1", "messageSeq": 1, "dispatchTargets": [{"agentId": AGENT, "eventKind": "user_request"}]}
    monkeypatch.setattr(work.messaging_repo, "get_agent_delivery", AsyncMock(return_value={"lastSeq": 0}))
    monkeypatch.setattr(work.messaging_repo, "get_room", AsyncMock(return_value={**META, "lastSeq": 1}))
    monkeypatch.setattr(work.messaging_repo, "list_messages_between", AsyncMock(return_value=[message]))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    advance = AsyncMock(return_value=True)
    monkeypatch.setattr(work.messaging_repo, "advance_agent_delivery", advance)
    publish = AsyncMock()
    monkeypatch.setattr(work, "publish_delivery_changed", publish)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_dispatch_message", AsyncMock(return_value=accepted))

    await worker._room_dispatch({"roomId": ROOM, "agentId": AGENT, "throughSeq": 1})

    advance.assert_awaited_once_with(ROOM, AGENT, 0, 1, accepted=accepted)
    assert publish.await_count == int(accepted)


@pytest.mark.asyncio
@pytest.mark.parametrize(("chosen", "visible_failure"), [(AGENT, True), ("another-agent", False), ("none", False)])
async def test_oversized_ambient_message_fails_only_the_jev_choice(monkeypatch, chosen, visible_failure):
    from core.services import jev

    _wire_dispatch(monkeypatch, created=True, redispatched=None)
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(return_value={"_oversized": True}))
    monkeypatch.setattr(jev, "route", AsyncMock(return_value={"who": chosen, "placement": "main"}))
    close = AsyncMock()
    monkeypatch.setattr(work, "_close_refused_run", close)
    message = {
        "messageId": "m1",
        "messageSeq": 1,
        "author": {"kind": "human"},
        "dispatchTargets": [
            {"agentId": AGENT, "eventKind": "room_event"},
            {"agentId": "another-agent", "eventKind": "room_event"},
        ],
    }

    await OpenClawWorkWorker(sqs_client=FakeSqs())._dispatch_message(META, MEMBER, message, "room_event")

    assert close.await_count == int(visible_failure)
    assert work.messaging_repo.create_run.await_count == int(visible_failure)


@pytest.mark.asyncio
async def test_every_message_including_the_first_gets_a_fresh_visibility_clock(monkeypatch):
    sqs = FakeSqs()
    worker = OpenClawWorkWorker(sqs_client=sqs)
    monkeypatch.setattr(worker, "_process_message", AsyncMock())

    await worker._process_group([{"ReceiptHandle": "rh-1"}, {"ReceiptHandle": "rh-2"}])

    assert [(c["ReceiptHandle"], c["VisibilityTimeout"]) for c in sqs.visibility_calls] == [
        ("rh-1", _VISIBILITY_SECONDS),
        ("rh-2", _VISIBILITY_SECONDS),
    ]


def test_visibility_outlasts_one_dispatch():
    # Jev's decide budget + its margin, a 10 s steer attempt, then the ingress budget.
    assert _VISIBILITY_SECONDS > 11 + 10 + _AGENTCORE_INGRESS_TIMEOUT_S
    # A crash before invoke must be retried from SQS before the ledger is swept.
    assert work.messaging_repo.RUN_DISPATCH_TIMEOUT_S > max(_VISIBILITY_SECONDS, 240)


@pytest.mark.asyncio
async def test_failed_visibility_refresh_never_invokes_or_skips_ahead_in_the_group(monkeypatch):
    class ExpiredReceipt(FakeSqs):
        def change_message_visibility(self, **kwargs):
            raise RuntimeError("receipt no longer owned")

    worker = OpenClawWorkWorker(sqs_client=ExpiredReceipt())
    process = AsyncMock()
    monkeypatch.setattr(worker, "_process_message", process)
    await worker._process_group([{"ReceiptHandle": "expired"}, {"ReceiptHandle": "later"}])
    process.assert_not_awaited()
