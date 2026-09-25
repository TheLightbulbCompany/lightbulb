"""A human follow-up to an agent whose run in the same conversation is live is steered into that
run -- no second run, no second WORK ingress -- and falls back to a normal turn otherwise."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker

ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
FIRST = "44444444-4444-4444-8444-444444444444"
FOLLOW = "77777777-7777-4777-8777-777777777777"
LIVE_RUN = "88888888-8888-4888-8888-888888888888"
META = {"ownerId": "org_1", "roomId": ROOM, "kind": "group-dm", "revision": 1, "deliveryKey": "k"}
MEMBER = {"kind": "agent", "id": AGENT}
AUTHOR = {"kind": "human", "id": "user_1", "displayName": "Ada"}
LIVE = {
    "runId": LIVE_RUN,
    "agentId": AGENT,
    "roomId": ROOM,
    "status": "started",
    "triggerMessageId": FIRST,
    "threadRootId": None,
    "dispatchedAt": "2026-09-25T01:46:37+00:00",
    "sweep": "open",
}


def _message(**over):
    return {
        "messageId": FOLLOW,
        "messageSeq": 5,
        "author": AUTHOR,
        "text": "also the supademo account",
        "threadRootMessageId": FIRST,
        **over,
    }


def _fixture(monkeypatch, *, runs, steer_status=202, steer_error=None):
    created, ingress, steers, published = [], [], [], []

    async def fake_request(container, path, **kwargs):
        steers.append((path, kwargs["json_body"]))
        if steer_error:
            raise steer_error
        return MagicMock(status_code=steer_status)

    async def fake_post(*args):
        ingress.append(args[2])
        answer = MagicMock(status_code=202)
        answer.json.return_value = {"status": "accepted"}
        return answer

    async def fake_create_run(owner_id, run_id, **fields):
        created.append((run_id, fields))
        return True

    publisher = MagicMock(publish_room=AsyncMock(side_effect=lambda room, event: published.append(event) or True))
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=MEMBER))
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(work.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=runs))
    monkeypatch.setattr(work.messaging_repo, "workspace_id_for_meta", AsyncMock(return_value="ws"))
    monkeypatch.setattr(work.messaging_repo, "create_run", fake_create_run)
    monkeypatch.setattr(work, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(work, "build_ingress_body", AsyncMock(return_value={"version": 1}))
    monkeypatch.setattr(work, "put_metric", MagicMock())
    monkeypatch.setattr(work, "get_live_event_publisher", lambda: publisher)
    monkeypatch.setattr(work.runtime_transport, "request", fake_request)
    from core.services import local_sessions

    monkeypatch.setattr(local_sessions, "live_sessions_for_room", AsyncMock(return_value=[]))
    worker = OpenClawWorkWorker(sqs_client=MagicMock())
    monkeypatch.setattr(worker, "_post", fake_post)
    return worker, created, ingress, steers, published


@pytest.mark.asyncio
async def test_a_follow_up_in_the_runs_thread_is_steered_not_dispatched(monkeypatch):
    worker, created, ingress, steers, published = _fixture(monkeypatch, runs=[LIVE])
    await worker._dispatch_message(META, MEMBER, _message(), "user_request")
    assert steers == [
        (
            f"/plugins/lightbulb-channel/runs/{LIVE_RUN}/steer",
            {"text": "Ada: also the supademo account", "messageId": FOLLOW},
        )
    ]
    assert created == [] and ingress == []  # one run: no second ledger row, no second ingress
    frame = published[0].payload["frame"] if hasattr(published[0], "payload") else published[0]["payload"]["frame"]
    assert frame == {"type": "steered", "runId": LIVE_RUN, "agentId": AGENT, "messageId": FOLLOW}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(404, None), (503, None), (None, httpx.ReadTimeout("slow"))])
async def test_a_refused_steer_falls_back_to_a_normal_turn(monkeypatch, status, error):
    worker, created, ingress, steers, published = _fixture(
        monkeypatch, runs=[LIVE], steer_status=status, steer_error=error
    )
    await worker._dispatch_message(META, MEMBER, _message(), "user_request")
    assert len(steers) == 1 and published == []
    assert len(created) == 1 and len(ingress) == 1
    assert created[0][1]["thread_root_id"] == FIRST  # where it replies: what the next follow-up matches


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runs,message,meta",
    [
        ([{**LIVE, "agentId": "someone-else"}], _message(), META),  # another agent's run
        ([{**LIVE, "status": "ended"}], _message(), META),  # already over
        ([LIVE], _message(threadRootMessageId="99999999-9999-4999-8999-999999999999"), {**META, "kind": "channel"}),
        ([LIVE], _message(author={"kind": "agent", "id": "x", "displayName": "Bot"}), META),  # agents never steer
        ([LIVE], _message(attachments=[{"id": "a"}]), META),  # a steer is text only
        ([LIVE, {**LIVE, "runId": "r2", "triggerMessageId": FOLLOW}], _message(), META),  # redelivery of its own run
    ],
)
async def test_what_is_not_a_follow_up_to_the_live_run_gets_its_own_turn(monkeypatch, runs, message, meta):
    worker, created, ingress, steers, _ = _fixture(monkeypatch, runs=runs)
    await worker._dispatch_message(meta, MEMBER, message, "user_request")
    assert steers == [] and len(created) == 1 and len(ingress) == 1


@pytest.mark.asyncio
async def test_in_a_dm_any_follow_up_steers_and_top_level_matches_a_main_conversation_run(monkeypatch):
    worker, created, _, steers, _ = _fixture(monkeypatch, runs=[LIVE])
    await worker._dispatch_message({**META, "kind": "dm"}, MEMBER, _message(threadRootMessageId=None), "user_request")
    assert len(steers) == 1 and created == []
    worker, created, _, steers, _ = _fixture(monkeypatch, runs=[LIVE])
    await worker._dispatch_message(META, MEMBER, _message(threadRootMessageId=None), "user_request")
    assert len(steers) == 1 and created == []  # LIVE answers in the main conversation (threadRootId None)
