"""The immutable Task gate covers the observable-row window, before any Question/update exists."""

from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import messaging_repo, questions_repo, tasks_repo
from core.services import meeting_followups, task_asks
from core.temporal import activities, task_questions
from core.temporal.task_questions import AnswerRef, AskRef
from .test_meeting_context import world  # noqa: F401
from ..super_agent.orchestration.test_tasks_repo import table  # noqa: F401

REAL_GET_TRIGGER = tasks_repo.get_trigger


@pytest.fixture
async def initial_task(world, table, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", AsyncMock(return_value="test-workspace-channel"))
    await tasks_repo.put_trigger("org_a", {**world["trigger"], "receivedAt": utc_now_iso()})
    monkeypatch.setattr(tasks_repo, "get_trigger", REAL_GET_TRIGGER)
    task_id = ts.new_task_id()
    brief, question = await meeting_followups.clarification(
        "org_a", world["trigger"], task_id, "Which report?", ["Weekly", "Monthly"]
    )
    ref = await task_asks.prepare_initial_question("org_a", task_id, "original-user", "shared", question)
    task, _ = await tasks_repo.create_task(
        "org_a",
        {
            "taskId": task_id,
            "identifier": "LB-1",
            "title": "Clarify meeting follow-up",
            "description": brief,
            "status": "queued",
            "audience": {"kind": "person", "userIds": ["original-user"]},
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
    result = await activities.begin_attempt("org_a", task["taskId"], ["chat:early"])
    assert result.n == 0 and result.initial_question.qid == question["qid"]
    assert result.initial_question.state == "pending"
    assert (await tasks_repo.get_task_tree("org_a", task["taskId"]))["attempts"] == []
    assert await questions_repo.get_question("org_a", task["taskId"], question["qid"]) is None


async def test_initial_reference_cannot_be_changed_after_task_creation(initial_task):
    task, _ = initial_task
    with pytest.raises(ValueError, match="initialQuestion"):
        await tasks_repo.update_task_fields(
            "org_a", task["taskId"], {"initialQuestion": {"qid": "different"}}, expected_version=task["version"]
        )


async def test_binding_rejects_a_different_principal_before_attempt_write(initial_task):
    task, _ = initial_task
    broken = {**task, "initialQuestion": {**task["initialQuestion"], "principalUserId": "someone-else"}}
    with pytest.raises(task_asks.AskRefused, match="binding"):
        await task_asks.initial_question("org_a", broken)
    assert (await tasks_repo.get_task_tree("org_a", task["taskId"]))["attempts"] == []


async def _open(task, question):
    return await ActivityEnvironment().run(
        task_questions.open_asked_question,
        "org_a",
        task["taskId"],
        AskRef(ts.trigger_id("ask", question["qid"]), "question", "question", True, True),
    )


async def test_cancelled_initial_question_never_releases_first_attempt(initial_task):
    task, question = initial_task
    await _open(task, question)
    await questions_repo.cancel_open("org_a", task["taskId"], by="original-user", reason="stopped")
    result = await activities.begin_attempt("org_a", task["taskId"], ["chat:yes"])
    assert result.n == 0 and result.initial_question.state == "cancelled"
    assert (await tasks_repo.get_task_tree("org_a", task["taskId"]))["attempts"] == []


async def test_answer_before_restart_replays_actual_answer_once_into_first_attempt(initial_task, monkeypatch):
    task, question = initial_task
    await _open(task, question)
    answer = await ActivityEnvironment().run(
        task_questions.record_answer,
        "org_a",
        task["taskId"],
        AnswerRef(question["qid"], "option-0", None, "original-user", "client-answer"),
    )
    monkeypatch.setattr(activities, "_session_kind", AsyncMock(return_value="workspace"))
    monkeypatch.setattr(activities, "_agent_for", AsyncMock(return_value=ts.new_task_id()))
    monkeypatch.setattr(activities, "_move", AsyncMock())
    monkeypatch.setattr(activities, "_announce", AsyncMock())
    for _ in range(2):
        result = await activities.begin_attempt("org_a", task["taskId"], ["chat:early", answer.trigger_id])
        assert result.n == 1 and result.initial_question is None
    tree = await tasks_repo.get_task_tree("org_a", task["taskId"])
    assert len(tree["attempts"]) == 1
    assert tree["attempts"][0]["inputTriggerIds"] == ["chat:early", answer.trigger_id]


async def test_same_task_retry_rejects_a_different_initial_reference(initial_task):
    task, _ = initial_task
    original = await REAL_GET_TRIGGER("org_a", task["triggerId"])
    changed = {**task["initialQuestion"], "principalUserId": "different-user"}
    with pytest.raises(ValueError, match="binding_changed"):
        await activities._ensure_task("org_a", task["taskId"], original, "shared", "queued", initialQuestion=changed)
    assert (await tasks_repo.get_task("org_a", task["taskId"]))["initialQuestion"] == task["initialQuestion"]


def test_initial_question_is_an_explicit_read_only_task_projection():
    schema = ts.Task.model_json_schema(by_alias=True)
    assert schema["properties"]["initialQuestion"]["readOnly"] is True
