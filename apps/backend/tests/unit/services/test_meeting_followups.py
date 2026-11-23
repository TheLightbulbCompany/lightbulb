"""One principal-aware system completion event, never blanket human-commitment authorization."""

from unittest.mock import AsyncMock

import pytest

from core.services import admission
from core.voice.meetings import meeting_followups as mod
from .test_meeting_context import world  # noqa: F401
from core.repositories import messaging_repo


async def test_completion_is_one_stable_nonhuman_trigger_under_original_principal(world):  # noqa: F811
    for _ in range(2):
        assert await mod.enqueue("original-user", "notes-request", world["request"])
    first, second = [c.args[1].model_dump(by_alias=True) for c in world["enqueue"].await_args_list]
    assert first["triggerId"] == second["triggerId"] == world["trigger"]["triggerId"]
    assert first["source"] == "webhook" and first["actorUserId"] == "original-user"
    assert first["ref"]["origin"] == "meeting" and first["ref"]["confirmAll"] is True
    assert "messageId" not in first["ref"] and "original-transcript" not in first["ref"]["text"]


async def test_admission_rechecks_setting_after_completion_was_enqueued(world, monkeypatch):  # noqa: F811
    signal = AsyncMock()
    monkeypatch.setattr(admission, "_signal", signal)
    monkeypatch.setattr(admission, "paused_room", AsyncMock(return_value=None))
    world["config"]["meetings"]["followUps"] = False
    await admission._admit_trigger("original-user", world["trigger"]["triggerId"])
    signal.assert_not_awaited()


def test_current_config_never_promises_automatic_notes_email():
    source = {"name": "Front door", "meetings": {"followUps": False, "emailNotes": True}}
    config = messaging_repo.voice_agent_config(source)
    assert config == {"name": "Front door", "meetings": {"followUps": False}}
    assert source["meetings"]["emailNotes"] is True  # no stored-row migration or mutation


@pytest.mark.parametrize(("principal", "admitted"), [("user_a", True), ("user_b", False)])
async def test_meeting_followups_owner_is_inviter(world, principal, admitted):  # noqa: F811
    """In a person's tenant the one person who may be the inviter is the tenant's person: their completed
    meeting admits follow-up work, and nobody else's does, even seated as the room's owner."""
    world["meta"]["ownerId"] = "user_a"
    world["member"]["id"] = principal
    world["request"]["principalUserId"] = principal
    world["trigger"]["actorUserId"] = principal
    assert await mod.enqueue("user_a", "notes-request", world["request"]) is admitted
