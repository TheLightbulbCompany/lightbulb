"""The three ways the room consumer delivered wrong: a redelivery re-ran a finished turn, one
undeliverable message deafened an agent in a room forever, and SQS handed a message to a second
consumer while the first was still invoking."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import openclaw_work as work
from core.services.openclaw_work import (
    _AGENTCORE_INGRESS_TIMEOUT_S,
    _POISON_ATTEMPTS,
    _VISIBILITY_SECONDS,
    OpenClawWorkWorker,
    PermanentDispatchError,
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
async def test_an_oversized_final_ingress_carries_its_run(monkeypatch):
    _wire_dispatch(monkeypatch, created=True, redispatched=None)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_post", AsyncMock(side_effect=ValueError("Lightbulb ingress exceeds 1 MiB")))

    with pytest.raises(PermanentDispatchError) as caught:
        await worker._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request")

    assert caught.value.reason == "oversize"
    assert caught.value.run_id == work.messaging_repo.run_id_for(AGENT, "m1")


def _wire_room(monkeypatch, failures: int):
    message = {
        "messageId": "m7",
        "messageSeq": 7,
        "roomId": ROOM,
        "dispatchTargets": [{"agentId": AGENT, "eventKind": "user_request"}],
    }
    monkeypatch.setattr(work.messaging_repo, "get_agent_delivery", AsyncMock(return_value={"lastSeq": 6}))
    monkeypatch.setattr(work.messaging_repo, "get_room", AsyncMock(return_value={**META, "lastSeq": 7}))
    monkeypatch.setattr(work.messaging_repo, "list_messages_between", AsyncMock(return_value=[message]))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    monkeypatch.setattr(work.messaging_repo, "record_delivery_failure", AsyncMock(return_value=failures))
    monkeypatch.setattr(work.messaging_repo, "end_run", AsyncMock(return_value={}))
    advance = AsyncMock(return_value=True)
    monkeypatch.setattr(work.messaging_repo, "advance_agent_delivery", advance)
    notice = AsyncMock()
    monkeypatch.setattr("core.services.messaging.post_system_notice", notice)
    monkeypatch.setattr(work, "publish_delivery_changed", AsyncMock())
    return advance, notice


@pytest.mark.asyncio
async def test_an_undeliverable_message_is_retried_then_skipped_with_a_notice(monkeypatch):
    pointer = {"roomId": ROOM, "agentId": AGENT, "throughSeq": 7}
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(
        worker,
        "_dispatch_message",
        AsyncMock(
            side_effect=PermanentDispatchError("plugin refused ingress (400)", reason="ingress_400", run_id="r7")
        ),
    )

    advance, notice = _wire_room(monkeypatch, failures=_POISON_ATTEMPTS - 1)
    with pytest.raises(PermanentDispatchError):
        await worker._room_dispatch(pointer)
    advance.assert_not_called()

    advance, notice = _wire_room(monkeypatch, failures=_POISON_ATTEMPTS)
    await worker._room_dispatch(pointer)
    advance.assert_awaited_once_with(ROOM, AGENT, 6, 7)  # the cursor moves on past it
    work.messaging_repo.end_run.assert_awaited_once_with(
        "owner_test", "r7", outcome="error", error_kind="undeliverable"
    )
    assert notice.await_args.kwargs["key"] == f"undeliverable:m7:{AGENT}"


@pytest.mark.asyncio
async def test_each_message_in_a_group_gets_a_fresh_visibility_clock(monkeypatch):
    sqs = FakeSqs()
    worker = OpenClawWorkWorker(sqs_client=sqs)
    monkeypatch.setattr(worker, "_process_message", AsyncMock())

    await worker._process_group([{"ReceiptHandle": "rh-1"}, {"ReceiptHandle": "rh-2"}])

    assert [(c["ReceiptHandle"], c["VisibilityTimeout"]) for c in sqs.visibility_calls] == [
        ("rh-2", _VISIBILITY_SECONDS)
    ]


def test_visibility_outlasts_one_dispatch():
    # Jev's decide budget + its margin, a 10 s steer attempt, then the ingress budget.
    assert _VISIBILITY_SECONDS > 11 + 10 + _AGENTCORE_INGRESS_TIMEOUT_S
