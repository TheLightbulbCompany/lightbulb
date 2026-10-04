"""Conversation engine §3.6: "don't post that". Both late-answer doors (the worker's
/late-answer and the bridge's agent-messages POST) use event id live-late:<runId>, so one guard
in create_agent_message covers both."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging
from tests.unit.services.test_agent_message_attachments import AGENT, OWNER, ROOM, created  # noqa: F401


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


async def test_a_mark_that_lands_before_the_commit_refuses_the_answer(created):  # noqa: F811
    """Codex #3663: the early read passed, then "don't post that" landed. The condition rides the message's
    own transaction, and its failure is the same refusal."""
    created.side_effect = messaging.messaging_repo.ExtraConditionFailed("marked")
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "live-late:run-9",
            "roomId": ROOM,
            "agentId": AGENT,
            "text": "Three PRs are open.",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    check = {"ConditionCheck": "run-9 may post"}
    with (
        patch.object(messaging.messaging_repo, "get_voice_run", AsyncMock(return_value={"roomId": ROOM})),
        patch.object(messaging.messaging_repo, "voice_run_may_post", lambda owner, run: check) as _,
    ):
        with pytest.raises(messaging.ApiProblem) as e:
            await messaging.create_agent_message(OWNER, request)
    assert e.value.error.code == "ACTION_UNAVAILABLE" and e.value.status_code == 409
    assert created.await_args.kwargs["extra_conditions"] == [check]
    messaging.put_metric.assert_any_call("voice.huddle.late_answer_suppressed")


async def test_an_ordinary_agent_message_carries_no_voice_condition(created):  # noqa: F811
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "evt-1",
            "roomId": ROOM,
            "agentId": AGENT,
            "text": "reply",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    await messaging.create_agent_message(OWNER, request)
    assert created.await_args.kwargs["extra_conditions"] is None


def test_two_agents_whose_names_slug_alike_keep_their_own_choice():
    choices = messaging._part_choices(["foo-bar", "foo bar"])
    alone = [names for _, names in choices.values() if len(names) == 1]
    assert {"foo-bar"} in alone and {"foo bar"} in alone and len(choices) == 4
