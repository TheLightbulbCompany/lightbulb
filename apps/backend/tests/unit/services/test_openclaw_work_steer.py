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


def _fixture(monkeypatch, *, runs, steer_status=202, steer_error=None, run_open=True):
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
    monkeypatch.setattr(work.messaging_repo, "add_steered_message", AsyncMock(return_value=run_open))
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


@pytest.mark.asyncio
async def test_a_steer_taken_by_a_run_that_ended_meanwhile_gets_its_own_turn(monkeypatch):
    worker, created, ingress, steers, published = _fixture(monkeypatch, runs=[LIVE], run_open=False)
    await worker._dispatch_message(META, MEMBER, _message(), "user_request")
    assert len(steers) == 1 and published == []  # no "saw this" receipt for a steer nobody will answer
    assert len(created) == 1 and len(ingress) == 1


@pytest.mark.asyncio
async def test_a_lost_steer_follow_up_is_dispatched_as_a_normal_turn_never_steered(monkeypatch):
    worker, created, ingress, steers, _ = _fixture(monkeypatch, runs=[LIVE])
    row = {"triggerMessageId": FOLLOW, "eventKind": "user_request", "reason": work.STEER_LOST, "text": ""}
    monkeypatch.setattr(work.messaging_repo, "get_follow_up", AsyncMock(return_value=row))
    monkeypatch.setattr(work.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(work.messaging_repo, "get_message_by_id", AsyncMock(return_value=_message()))
    await worker._room_follow_up({"roomId": ROOM, "agentId": AGENT, "followUpId": "f1"})
    assert steers == [] and len(created) == 1 and len(ingress) == 1
    assert created[0][1]["trigger_message_id"] == FOLLOW


# Routing v2 §4.1: on a room_event target the agent's own live run hears the line before Jev.
AMBIENT = {**LIVE, "eventKind": "room_event"}
ADDRESSED = {**LIVE, "eventKind": "user_request"}


def _jev(monkeypatch, decision=None):
    from core.services import jev

    route = AsyncMock(return_value=decision or {"who": "none"})
    monkeypatch.setattr(jev, "route", route)
    return route


@pytest.mark.asyncio
async def test_a_room_event_line_steers_the_agents_ambient_run_before_jev_is_asked(monkeypatch):
    worker, created, ingress, steers, _ = _fixture(monkeypatch, runs=[AMBIENT])
    route = _jev(monkeypatch)
    assert await worker._dispatch_message(META, MEMBER, _message(text="$42k"), "room_event") is True
    route.assert_not_called()
    assert created == [] and ingress == []
    text = steers[0][1]["text"]
    assert text.startswith("Ada: $42k — said since you were asked.")
    assert "reply exactly [SILENT]" in text and text.endswith("If it is unrelated, carry on.")
    work.put_metric.assert_any_call("messaging.run.steered", dimensions={"outcome": "steered", "kind": "ambient"})


@pytest.mark.asyncio
async def test_an_addressed_run_gets_the_bare_line_never_the_stand_down(monkeypatch):
    worker, _, _, steers, _ = _fixture(monkeypatch, runs=[ADDRESSED])
    route = _jev(monkeypatch)
    assert await worker._dispatch_message(META, MEMBER, _message(text="$42k"), "room_event") is True
    route.assert_not_called()
    assert steers[0][1]["text"] == "Ada: $42k"
    work.put_metric.assert_any_call("messaging.run.steered", dimensions={"outcome": "steered", "kind": "addressed"})


@pytest.mark.asyncio
async def test_no_live_run_in_this_conversation_goes_to_jev_and_is_not_steered_again(monkeypatch):
    other_thread = "99999999-9999-4999-8999-999999999999"
    worker, created, ingress, steers, _ = _fixture(monkeypatch, runs=[AMBIENT])
    route = _jev(monkeypatch, {"who": AGENT, "placement": "main"})
    meta = {**META, "kind": "channel"}
    await worker._dispatch_message(meta, MEMBER, _message(threadRootMessageId=other_thread), "room_event")
    route.assert_awaited_once()
    assert steers == [] and len(created) == 1 and len(ingress) == 1


@pytest.mark.asyncio
async def test_a_dm_room_event_steers_any_live_run_of_this_agent(monkeypatch):
    worker, created, _, steers, _ = _fixture(monkeypatch, runs=[AMBIENT])
    route = _jev(monkeypatch)
    await worker._dispatch_message({**META, "kind": "dm"}, MEMBER, _message(threadRootMessageId=None), "room_event")
    route.assert_not_called()
    assert len(steers) == 1 and created == []


@pytest.mark.asyncio
async def test_a_removed_member_is_never_steered(monkeypatch):
    worker, created, _, steers, _ = _fixture(monkeypatch, runs=[AMBIENT])
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value=None))
    _jev(monkeypatch)
    assert await worker._dispatch_message(META, MEMBER, _message(), "room_event") is False
    assert steers == [] and created == []
