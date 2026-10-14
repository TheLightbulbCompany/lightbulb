"""T35: the 30-minute label on an ended room run, and its one event."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import run_feedback

ENDED = "2026-10-14T10:00:00+00:00"
RUN = {"runId": "r1", "endedAt": ENDED, "outcome": "completed", "threadRootId": None}
TRIGGER = {"messageId": "m1", "messageSeq": 4, "author": {"kind": "human", "id": "user_a"}}


def _msg(kind, author, at, *, run_id=None, reactions=None, thread=None):
    return {
        "author": {"kind": kind, "id": author},
        "createdAt": at,
        "runId": run_id,
        "reactions": reactions or {},
        "threadRootMessageId": thread,
    }


REPLY = _msg("agent", "a1", "2026-10-14T09:59:59+00:00", run_id="r1")


def test_reacted_beats_everything():
    reply = {**REPLY, "reactions": {"👍": ["user_a"]}}
    assert run_feedback.classify(RUN, TRIGGER, [reply]) == "reacted"


def test_a_human_after_a_reply_is_replied():
    later = [REPLY, _msg("human", "user_b", "2026-10-14T10:05:00+00:00")]
    assert run_feedback.classify(RUN, TRIGGER, later) == "replied"


def test_the_requester_again_after_a_stop_is_re_asked():
    stopped = {**RUN, "outcome": "aborted"}
    later = [REPLY, _msg("human", "user_a", "2026-10-14T10:01:00+00:00")]
    assert run_feedback.classify(stopped, TRIGGER, later) == "re_asked"


def test_nothing_within_thirty_minutes_is_ignored():
    later = [REPLY, _msg("human", "user_b", "2026-10-14T10:45:00+00:00")]
    assert run_feedback.classify(RUN, TRIGGER, later) == "ignored"


def test_another_thread_does_not_count():
    later = [REPLY, _msg("human", "user_b", "2026-10-14T10:05:00+00:00", thread="other")]
    assert run_feedback.classify(RUN, TRIGGER, later) == "ignored"


@pytest.mark.asyncio
async def test_a_due_run_is_labelled_once_and_emits_one_event():
    row = {
        **RUN,
        "sweep": "followup",
        "roomId": "room",
        "agentId": "a1",
        "triggerMessageId": "m1",
        "workspaceId": "org_1",
        "eventKind": "room_event",
        "routedBy": "jev",
        "routingP": 0.8,
        "abortRequestedAt": "x",
        "stoppedBy": "user_a",
        "stopAfterS": 9,
    }
    repo = run_feedback.messaging_repo
    with (
        patch.object(repo, "list_overdue_runs", AsyncMock(return_value=[{"ownerId": "org_1", "runId": "r1"}])) as due,
        patch.object(repo, "get_run", AsyncMock(return_value=row)),
        patch.object(repo, "get_message_by_id", AsyncMock(return_value=TRIGGER)),
        patch.object(repo, "list_messages", AsyncMock(return_value=([REPLY], None))),
        patch.object(repo, "label_run_followup", AsyncMock(return_value={"followUp": "ignored"})) as label,
        patch.object(run_feedback, "capture_product_event") as capture,
        patch.object(run_feedback, "put_metric"),
    ):
        assert await run_feedback.label_due_runs_once(now_epoch=1) == 1
    assert due.await_args.kwargs == {"marker": "followup"}
    label.assert_awaited_once_with("org_1", "r1", "ignored")
    kw = capture.call_args.kwargs
    assert kw["event"] == "room_run_feedback" and kw["distinct_id"] == "user_a"
    props = kw["properties"]
    assert props["routed_by"] == "jev" and props["routing_p"] == 0.8
    assert props["stopped"] is True and props["stopped_by"] == "user_a" and props["stop_after_s"] == 9
    assert props["workspace_id"] == "org_1" and props["follow_up"] == "ignored"
