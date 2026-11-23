"""Original source pages stay scoped, bounded and immutable across recurring occurrences."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import admission
from core.voice.meetings import meeting_context as mod, meeting_notes
from core.repositories import messaging_repo
from core.repositories import tasks_repo


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "meeting-context-test-key-with-32-bytes-minimum")
    request = {
        "roomId": "meeting-room",
        "occurrence": 100,
        "principalUserId": "original-user",
        "meeting": {
            "inCallAt": 101,
            "endedAt": 200,
            "transcriptId": "original-transcript",
            "transcriptRoomId": "notes-room",
        },
    }
    trigger = {
        "triggerId": "meeting-followups:meeting-room:100",
        "actorUserId": "original-user",
        "source": "webhook",
        "admission": "candidate",
        "lane": "shared",
        "ref": {
            "roomId": "meeting-room",
            "origin": "meeting",
            "confirmAll": True,
            "meetingFollowups": {"requestId": "notes-request", "occurrence": 100, "meetingRoomId": "meeting-room"},
        },
    }
    state = {
        "request": request,
        "trigger": trigger,
        "meta": {
            "ownerId": "original-user",
            "meeting": {
                "startAt": 300,
                "transcriptId": "next-transcript",
                "history": [{"startAt": 100, "transcriptId": "original-transcript"}],
            },
        },
        "member": {"id": "original-user", "kind": "human", "role": "owner"},
        "members": [{"user_id": "original-user", "role": "org:member"}],
        "config": {"meetings": {"followUps": True}},
    }
    monkeypatch.setattr(mod.store, "get_voice_agent", AsyncMock(side_effect=lambda _: state["config"]))
    monkeypatch.setattr(mod.store, "get_meeting_notes_request", AsyncMock(side_effect=lambda *_: state["request"]))
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(side_effect=lambda _: state["meta"]))
    monkeypatch.setattr(messaging_repo, "get_member", AsyncMock(side_effect=lambda *_: state["member"]))
    # the room's members: the original inviter's seat (state["member"]) when there is one
    monkeypatch.setattr(
        messaging_repo, "list_members", AsyncMock(side_effect=lambda *_: [state["member"]] if state["member"] else [])
    )
    monkeypatch.setattr(tasks_repo, "get_trigger", AsyncMock(side_effect=lambda *_: state["trigger"]))
    state["enqueue"] = AsyncMock(return_value=True)
    monkeypatch.setattr(admission, "enqueue_trigger", state["enqueue"])
    state["load"] = MagicMock(return_value=b"human source " * 3000)
    monkeypatch.setattr(mod.transcripts, "get", state["load"])
    state["tasks"] = [
        {
            "taskId": f"task-{i:02}",
            "title": "Already requested work",
            "description": "d" * 1000,
            "status": "resolved" if i % 2 else "working",
        }
        for i in range(12)
    ]
    monkeypatch.setattr(meeting_notes, "_meeting_tasks", AsyncMock(side_effect=lambda *_: state["tasks"]))
    state["runId"] = "current-run"
    state["grant"] = mod.mint_grant("original-user", state["runId"], [trigger["triggerId"]])
    return state


async def test_a_gone_transcript_reads_as_none(world):
    world["load"].return_value = None
    assert (
        await mod.context(
            "original-user", world["runId"], world["trigger"]["triggerId"], "transcript", grant=world["grant"]
        )
        is None
    )


async def test_superseded_series_reads_only_the_original_occurrences_artifact(world):
    result = await mod.context(
        "original-user", world["runId"], world["trigger"]["triggerId"], "transcript", grant=world["grant"]
    )
    world["load"].assert_called_once_with("original-user", "notes-room", "original-transcript")
    assert result["quotedSource"] is True and len(result["text"]) == mod.PAGE_CHARS
    assert result["truncated"] and result["nextCursor"]
    next_page = await mod.context(
        "original-user",
        world["runId"],
        world["trigger"]["triggerId"],
        "transcript",
        result["nextCursor"],
        grant=world["grant"],
    )
    assert next_page["offset"] == mod.PAGE_CHARS


async def test_context_paginates_open_and_done_task_ids_before_new_work(world):
    page = await mod.context(
        "original-user", world["runId"], world["trigger"]["triggerId"], "tasks", grant=world["grant"]
    )
    assert {t["status"] for t in page["tasks"]} == {"working", "resolved"}
    assert len(page["tasks"]) == mod.TASK_PAGE and all(len(t["description"]) == 500 for t in page["tasks"])
    rest = await mod.context(
        "original-user",
        world["runId"],
        world["trigger"]["triggerId"],
        "tasks",
        page["nextCursor"],
        grant=world["grant"],
    )
    assert len(rest["tasks"]) == 2 and rest["nextCursor"] is None


async def test_cursor_cannot_switch_artifact_or_section(world):
    cursor = (
        await mod.context(
            "original-user", world["runId"], world["trigger"]["triggerId"], "transcript", grant=world["grant"]
        )
    )["nextCursor"]
    with pytest.raises(ValueError, match="cursor"):
        await mod.context(
            "original-user", world["runId"], world["trigger"]["triggerId"], "tasks", cursor, grant=world["grant"]
        )
    world["request"]["meeting"]["transcriptId"] = "another-artifact"
    world["meta"]["meeting"]["history"][0]["transcriptId"] = "another-artifact"
    with pytest.raises(ValueError, match="cursor"):
        await mod.context(
            "original-user", world["runId"], world["trigger"]["triggerId"], "transcript", cursor, grant=world["grant"]
        )


@pytest.mark.parametrize("change", ["owner", "actor", "occurrence", "artifact", "no_history"])
async def test_context_scope_cannot_be_retargeted(world, change):
    if change == "owner":
        world["meta"]["ownerId"] = "org_other"
    elif change == "actor":
        world["trigger"]["actorUserId"] = "new-inviter"
    elif change == "occurrence":
        world["trigger"]["ref"]["meetingFollowups"]["occurrence"] = 300
    elif change == "artifact":
        world["meta"]["meeting"]["history"][0]["transcriptId"] = "other"
    else:
        world["meta"]["meeting"]["history"] = []
    with pytest.raises(mod.NotAuthorized):
        await mod.context(
            "original-user", world["runId"], world["trigger"]["triggerId"], "transcript", grant=world["grant"]
        )
    world["load"].assert_not_called()


@pytest.mark.parametrize("change", ["other_run", "other_trigger", "other_owner", "wrong_purpose", "expired", "missing"])
async def test_private_source_grant_is_bound_to_this_run_batch_and_purpose(world, change):
    import jwt

    run, trigger, owner, token = world["runId"], world["trigger"]["triggerId"], "original-user", world["grant"]
    if change == "other_run":
        run = "another-private-router-run"
    elif change == "other_trigger":
        trigger = "another-private-completion"
    elif change == "other_owner":
        owner = "org_other"
    elif change == "missing":
        token = None
    else:
        claims = jwt.decode(token, mod._signing_key(), algorithms=["HS256"], audience=mod.GRANT_AUDIENCE)
        if change == "wrong_purpose":
            claims["kind"] = "some_other_grant"
        else:
            claims.update(iat=1, exp=2)
        token = jwt.encode(claims, mod._signing_key(), algorithm="HS256")
    with pytest.raises(mod.NotAuthorized, match="grant_refused"):
        await mod.context(owner, run, trigger, "transcript", grant=token)
    world["load"].assert_not_called()
