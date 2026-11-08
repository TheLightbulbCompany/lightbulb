"""A Meet line passed into an existing task (the Router's pass_through, or a thread reply) makes that task
confirmAll first: anyone in a meeting speaks as the inviter, so "go ahead and send it" never sends without a card."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import tasks_repo
from core.services import chat_gate, task_conversation

MEETING = {"actorUserId": "user_maya", "ref": {"roomId": "r", "origin": "meeting"}}


@pytest.fixture
def gate():
    with patch.object(chat_gate, "gate_chat", AsyncMock(return_value={"blocked": False})):
        yield


async def test_a_meeting_line_into_a_task_sets_confirm_all(gate):
    rows = [{"taskId": "k1", "version": 3}, {"taskId": "k1", "version": 4}]
    with (
        patch.object(tasks_repo, "get_task", AsyncMock(side_effect=rows)),
        patch.object(
            tasks_repo, "update_task_fields", AsyncMock(side_effect=[tasks_repo.VersionConflict("moved"), {}])
        ) as upd,
    ):
        assert await task_conversation.gate_input("org_1", "k1", MEETING) is None
    assert upd.await_args.args == ("org_1", "k1", {"confirmAll": True})
    assert upd.await_args.kwargs == {"expected_version": 4}  # a moved row is read again


async def test_a_task_already_confirm_all_and_a_typed_line_are_left_alone(gate):
    with (
        patch.object(
            tasks_repo, "get_task", AsyncMock(return_value={"taskId": "k1", "version": 1, "confirmAll": True})
        ),
        patch.object(tasks_repo, "update_task_fields", AsyncMock()) as upd,
    ):
        await task_conversation.gate_input("org_1", "k1", MEETING)
        await task_conversation.gate_input("org_1", "k1", {"actorUserId": "user_maya", "ref": {"roomId": "dm"}})
    upd.assert_not_awaited()


async def test_it_fails_closed_when_the_flag_cannot_be_set(gate):
    with (
        patch.object(tasks_repo, "get_task", AsyncMock(return_value={"taskId": "k1", "version": 1})),
        patch.object(tasks_repo, "update_task_fields", AsyncMock(side_effect=tasks_repo.VersionConflict("x"))),
        pytest.raises(RuntimeError),
    ):
        await task_conversation.gate_input("org_1", "k1", MEETING)
