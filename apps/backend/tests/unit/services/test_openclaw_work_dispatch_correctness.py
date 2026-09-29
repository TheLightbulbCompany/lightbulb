"""The three ways the room consumer delivered wrong: a redelivery re-ran a finished turn, one
undeliverable message deafened an agent in a room forever, and SQS handed a message to a second
consumer while the first was still invoking."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker

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
        monkeypatch.setattr(work, "build_ingress_body", AsyncMock(side_effect=ValueError("ingress exceeds 1 MiB")))
    else:
        post.side_effect = ValueError("ingress exceeds 1 MiB")

    await worker._dispatch_message(
        META, MEMBER, {"messageId": "m1", "messageSeq": 1}, "user_request", follow_up=follow_up
    )

    key = "m1" if follow_up is None else "m1:followup:f1"
    close.assert_awaited_once_with("owner_test", work.messaging_repo.run_id_for(AGENT, key), 413, "user_request")
    if stage == "body":
        post.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversize_redelivery_does_not_close_an_existing_run(monkeypatch):
    _wire_dispatch(monkeypatch, created=False, redispatched=None)
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(side_effect=ValueError("ingress exceeds 1 MiB")))
    close = AsyncMock()
    monkeypatch.setattr(work, "_close_refused_run", close)
    await OpenClawWorkWorker(sqs_client=FakeSqs())._dispatch_message(META, MEMBER, {"messageId": "m1"}, "user_request")
    close.assert_not_awaited()
