"""Conversation engine §3.6: "don't post that". Both late-answer doors (the worker's
/late-answer and the bridge's agent-messages POST) use event id live-late:<runId>, so one guard
in create_agent_message covers both."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging


def _req(event_id):
    return messaging.AgentMessageRequest(
        event_id=event_id,
        room_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        text="Three PRs are open.",
        reply_to_message_id=None,
        thread_root_message_id=None,
    )


async def test_a_no_post_runs_late_answer_is_refused_by_either_door():
    with (
        patch.object(
            messaging.messaging_repo, "get_voice_run", AsyncMock(return_value={"roomId": "r", "noPost": True})
        ) as row,
        patch.object(messaging.messaging_repo, "get_room", AsyncMock()) as room,
        patch.object(messaging, "put_metric") as metric,
    ):
        with pytest.raises(messaging.ApiProblem) as e:
            await messaging.create_agent_message("org_a", _req("live-late:run-9"))
    assert e.value.error.code == "ACTION_UNAVAILABLE" and e.value.status_code == 409  # the bridge never retries a 4xx
    row.assert_awaited_once_with("org_a", "run-9")
    room.assert_not_awaited()
    metric.assert_any_call("voice.huddle.late_answer_suppressed")


async def test_a_late_answer_without_no_post_takes_todays_path():
    with (
        patch.object(messaging.messaging_repo, "get_voice_run", AsyncMock(return_value={"roomId": "r"})),
        patch.object(messaging.messaging_repo, "get_room", AsyncMock(return_value=None)),
    ):
        with pytest.raises(messaging.ApiProblem) as e:
            await messaging.create_agent_message("org_a", _req("live-late:run-9"))
    assert e.value.error.code == "ROOM_NOT_FOUND"  # past the guard, into today's checks


async def test_other_agent_messages_never_read_a_voice_run():
    with (
        patch.object(messaging.messaging_repo, "get_voice_run", AsyncMock()) as row,
        patch.object(messaging.messaging_repo, "get_room", AsyncMock(return_value=None)),
    ):
        with pytest.raises(messaging.ApiProblem):
            await messaging.create_agent_message("org_a", _req("evt-1"))
    row.assert_not_awaited()
