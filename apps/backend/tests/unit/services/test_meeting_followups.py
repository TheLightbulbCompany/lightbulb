"""One principal-aware system completion event, never blanket human-commitment authorization."""

from unittest.mock import AsyncMock

import pytest

from core.services import admission, meeting_followups as mod, meeting_context
from .test_meeting_context import world  # noqa: F401


async def test_completion_is_one_stable_nonhuman_trigger_under_original_principal(world):  # noqa: F811
    for _ in range(2):
        assert await mod.enqueue("org_a", "notes-request", world["request"])
    first, second = [c.args[1].model_dump(by_alias=True) for c in world["enqueue"].await_args_list]
    assert first["triggerId"] == second["triggerId"] == world["trigger"]["triggerId"]
    assert first["source"] == "webhook" and first["actorUserId"] == "original-user"
    assert first["ref"]["origin"] == "meeting" and first["ref"]["confirmAll"] is True
    assert "messageId" not in first["ref"] and "original-transcript" not in first["ref"]["text"]


@pytest.mark.parametrize(
    "change", ["optout", "left_workspace", "left_room", "demoted", "replaced_member", "missing_original", "completed"]
)
async def test_revoked_optout_or_completed_occurrences_never_admit_new_work(world, change):  # noqa: F811
    if change == "optout":
        world["config"]["meetings"]["followUps"] = False
    elif change == "left_workspace":
        world["members"] = [{"user_id": "new-inviter"}]
    elif change == "left_room":
        world["member"] = None
    elif change == "demoted":
        world["member"]["role"] = "member"
    elif change == "replaced_member":
        world["member"]["id"] = "new-inviter"
    elif change == "missing_original":
        world["request"].pop("principalUserId")
    else:
        world["request"]["mailAttemptedAt"] = 1
    assert not await mod.enqueue("org_a", "notes-request", world["request"])
    world["enqueue"].assert_not_awaited()


async def test_admission_rechecks_setting_after_completion_was_enqueued(world, monkeypatch):  # noqa: F811
    signal = AsyncMock()
    monkeypatch.setattr(admission, "_signal", signal)
    monkeypatch.setattr(admission, "paused_room", AsyncMock(return_value=None))
    world["config"]["meetings"]["followUps"] = False
    await admission._admit_trigger("org_a", world["trigger"]["triggerId"])
    signal.assert_not_awaited()


def test_current_config_never_promises_automatic_notes_email():
    source = {"name": "Front door", "meetings": {"followUps": False, "emailNotes": True}}
    config = mod.messaging_repo.voice_agent_config(source)
    assert config == {"name": "Front door", "meetings": {"followUps": False}}
    assert source["meetings"]["emailNotes"] is True  # no stored-row migration or mutation


async def test_task_creation_rechecks_principal_before_any_new_task_write(world, monkeypatch):  # noqa: F811
    from core.temporal import activities

    world["members"] = []
    monkeypatch.setattr(activities.tasks_repo, "get_task", AsyncMock(return_value=None))
    create = AsyncMock()
    monkeypatch.setattr(activities.tasks_repo, "create_task", create)
    with pytest.raises(meeting_context.NotAuthorized, match="left_workspace"):
        await activities._ensure_task("org_a", "new-task", world["trigger"], "shared", "queued", title="Research")
    create.assert_not_awaited()


async def test_real_task_provenance_links_only_original_history_not_a_matching_commitment(world, monkeypatch):  # noqa: F811
    from core.services import meeting_invites

    original = world["meta"]["meeting"]["history"][0]
    original["followUps"] = [{"id": "human-commitment", "text": "Research", "status": "recorded"}]

    async def patch(room_id, fields):
        assert room_id == "meeting-room"
        meeting = world["meta"]["meeting"]
        meeting.update(fields(meeting))
        return meeting

    monkeypatch.setattr(meeting_invites, "_patch_meeting", patch)
    task = {
        "pk": "WS#org_a",
        "taskId": "actual-task",
        "title": "Research",
        "status": "working",
        "ownerUserId": "original-user",
        "origin": "meeting",
        "triggerId": world["trigger"]["triggerId"],
    }
    assert await mod.record_task("org_a", task)
    assert await mod.record_task("org_a", {**task, "status": "resolved"})
    history = world["meta"]["meeting"]["history"][0]["followUps"]
    assert history[0] == {"id": "human-commitment", "text": "Research", "status": "recorded"}
    assert len(history) == 2 and history[1]["taskId"] == "actual-task" and history[1]["status"] == "done"
    assert not world["meta"]["meeting"].get("followUps")


async def test_clarification_keeps_original_source_bounded_and_inviter_policy(world):  # noqa: F811
    from core.services import questions

    original = world["meta"]["meeting"]["history"][0]
    original["notes"] = "Original notes " * 1000
    world["meta"]["meeting"]["notes"] = "Next occurrence private notes"
    brief, ask = await mod.clarification("org_a", world["trigger"], "task-1", "Which report?", ["Weekly", "Monthly"])
    assert "Original notes" in brief and "Notes truncated" in brief
    assert "Next occurrence" not in brief and "original-transcript" in brief
    assert ask["kind"] == "question" and ask["attempt"] is None and ask["run_id"] is None and ask["blocking"]
    task = {"ownerUserId": "original-user", "origin": "meeting", "confirmAll": True}
    answerers, rule, base = await questions.answerers("org_a", task, ask["kind"], ask["subkind"])
    assert answerers == base == ["original-user"]
    question = {"answerers": answerers, "answererRule": rule, "baseAnswerers": base, "subkind": "question"}
    assert await questions.can_answer_now("org_a", question, "original-user")
    assert not await questions.can_answer_now("org_a", question, "new-inviter")
    world["members"] = []
    assert not await questions.can_answer_now("org_a", question, "original-user")
