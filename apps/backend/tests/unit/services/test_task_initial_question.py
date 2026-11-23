"""The immutable Task gate covers the observable-row window, before any Question/update exists."""

from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo, questions_repo, tasks_repo
from core.voice import store
from core.services import task_asks
from core.voice.meetings import meeting_followups
from core.temporal import activities, task_questions
from core.temporal.task_questions import AskRef
from .test_meeting_context import world  # noqa: F401
from ..super_agent.orchestration.test_tasks_repo import table  # noqa: F401

REAL_GET_TRIGGER = tasks_repo.get_trigger


@pytest.fixture
async def initial_task(world, table, monkeypatch):  # noqa: F811
    room_id = ts.new_task_id()
    world["request"]["roomId"] = room_id
    world["trigger"]["ref"]["roomId"] = room_id
    world["trigger"]["ref"]["meetingFollowups"]["meetingRoomId"] = room_id
    world["meta"]["roomId"] = room_id
    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", AsyncMock(return_value="test-workspace-channel"))
    await tasks_repo.put_trigger("original-user", {**world["trigger"], "receivedAt": utc_now_iso()})
    monkeypatch.setattr(tasks_repo, "get_trigger", REAL_GET_TRIGGER)
    task_id = ts.new_task_id()
    brief, question = await meeting_followups.clarification(
        "original-user", world["trigger"], task_id, "Which report?", ["Weekly", "Monthly"]
    )
    ref = await task_asks.prepare_initial_question("original-user", task_id, "original-user", "shared", question)
    task, _ = await tasks_repo.create_task(
        "original-user",
        {
            "taskId": task_id,
            "identifier": "LB-1",
            "title": "Clarify meeting follow-up",
            "description": brief,
            "status": "queued",
            "audience": {"kind": "person", "userIds": ["original-user"], "roomId": room_id},
            "lane": "shared",
            "ownerUserId": "original-user",
            "triggerId": world["trigger"]["triggerId"],
            "origin": "meeting",
            "confirmAll": True,
            "initialQuestion": ref,
        },
    )
    return task, question


async def test_first_visible_task_input_gets_question_ref_without_any_attempt_write(initial_task):
    task, question = initial_task
    result = await activities.begin_attempt("original-user", task["taskId"], ["chat:early"])
    assert result.n == 0 and result.initial_question.qid == question["qid"]
    assert result.initial_question.state == "pending"
    assert (await tasks_repo.get_task_tree("original-user", task["taskId"]))["attempts"] == []
    assert await questions_repo.get_question("original-user", task["taskId"], question["qid"]) is None


async def test_initial_reference_cannot_be_changed_after_task_creation(initial_task):
    task, _ = initial_task
    with pytest.raises(ValueError, match="initialQuestion"):
        await tasks_repo.update_task_fields(
            "original-user", task["taskId"], {"initialQuestion": {"qid": "different"}}, expected_version=task["version"]
        )


async def test_binding_rejects_a_different_principal_before_attempt_write(initial_task):
    task, _ = initial_task
    broken = {**task, "initialQuestion": {**task["initialQuestion"], "principalUserId": "someone-else"}}
    with pytest.raises(task_asks.AskRefused, match="binding"):
        await task_asks.initial_question("original-user", broken)
    assert (await tasks_repo.get_task_tree("original-user", task["taskId"]))["attempts"] == []


async def _open(task, question):
    return await ActivityEnvironment().run(
        task_questions.open_asked_question,
        "original-user",
        task["taskId"],
        AskRef(ts.trigger_id("ask", question["qid"]), "question", "question", True, True),
    )


async def test_cancelled_initial_question_never_releases_first_attempt(initial_task):
    task, question = initial_task
    await _open(task, question)
    await questions_repo.cancel_open("original-user", task["taskId"], by="original-user", reason="stopped")
    result = await activities.begin_attempt("original-user", task["taskId"], ["chat:yes"])
    assert result.n == 0 and result.initial_question.state == "cancelled"
    assert (await tasks_repo.get_task_tree("original-user", task["taskId"]))["attempts"] == []


async def test_same_task_retry_rejects_a_different_initial_reference(initial_task):
    task, _ = initial_task
    original = await REAL_GET_TRIGGER("original-user", task["triggerId"])
    changed = {**task["initialQuestion"], "principalUserId": "different-user"}
    with pytest.raises(ValueError, match="binding_changed"):
        await activities._ensure_task(
            "original-user", task["taskId"], original, "shared", "queued", initialQuestion=changed
        )
    assert (await tasks_repo.get_task("original-user", task["taskId"]))["initialQuestion"] == task["initialQuestion"]


def test_initial_question_is_an_explicit_read_only_task_projection():
    schema = ts.Task.model_json_schema(by_alias=True)
    assert schema["properties"]["initialQuestion"]["readOnly"] is True


@pytest.mark.parametrize("future", ["notes_expired", "followups_disabled"])
async def test_admitted_task_question_outlives_notes_request_and_future_preference(
    initial_task,
    world,  # noqa: F811
    monkeypatch,
    future,
):  # noqa: F811
    task, _ = initial_task
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(store, "get_meeting_notes_request", lookup)
    if future == "followups_disabled":
        world["config"]["meetings"]["followUps"] = False
    result = await activities.begin_attempt("original-user", task["taskId"], ["chat:after-seven-days"])
    assert result.n == 0 and result.initial_question.state == "pending"
    lookup.assert_not_awaited()


async def test_initial_question_binding_uses_stored_qid(world, table, monkeypatch):  # noqa: F811
    """The product builds the qid and passes it in the ask; the engine binds the Task to the qid stored on it (and
    the ask trigger that qid names), not to a product's prefix."""
    room_id = ts.new_task_id()
    world["trigger"]["ref"]["roomId"] = room_id
    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", AsyncMock(return_value="test-workspace-channel"))
    await tasks_repo.put_trigger("original-user", {**world["trigger"], "receivedAt": utc_now_iso()})
    monkeypatch.setattr(tasks_repo, "get_trigger", REAL_GET_TRIGGER)
    task_id = ts.new_task_id()
    question = {
        "qid": "q-chosen-by-the-product",
        "kind": "question",
        "subkind": "question",
        "title": "Which report?",
        "text": "Which report?",
        "options": [{"id": "option-0", "label": "Weekly"}],
        "blocking": True,
        "before_attempt": True,
        "attempt": None,
        "run_id": None,
    }
    ref = await task_asks.prepare_initial_question("original-user", task_id, "original-user", "shared", question)
    assert ref["qid"] == "q-chosen-by-the-product"
    task, _ = await tasks_repo.create_task(
        "original-user",
        {
            "taskId": task_id,
            "identifier": "LB-1",
            "title": "Clarify",
            "status": "queued",
            "audience": {"kind": "person", "userIds": ["original-user"], "roomId": room_id},
            "lane": "shared",
            "ownerUserId": "original-user",
            "triggerId": world["trigger"]["triggerId"],
            "origin": "meeting",
            "initialQuestion": ref,
        },
    )
    assert task_asks._initial_binding(task).qid == "q-chosen-by-the-product"
    elsewhere = {**task, "initialQuestion": {**ref, "triggerId": ts.trigger_id("ask", "another-qid")}}
    with pytest.raises(task_asks.AskRefused, match="binding"):
        task_asks._initial_binding(elsewhere)
    with pytest.raises(task_asks.AskRefused, match="invalid_initial_question"):
        await task_asks.prepare_initial_question(
            "original-user", task_id, "original-user", "shared", {**question, "qid": ""}
        )
