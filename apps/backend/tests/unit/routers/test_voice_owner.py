"""Owner mode's backend: only a member seated in the DM acts, one task per ask, reads with the member's scope."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from routers import voice_owner

ROOM = "7f1d2c3b-0000-4000-8000-000000000002"
MEMBERS = [{"kind": "human", "id": "user_maya", "role": "owner"}, {"kind": "agent", "id": "a1", "displayName": "Nova"}]
META = {"ownerId": "org_1", "kind": "dm"}


_TRIGGERS: set[str] = set()


async def _start(owner_id, body):
    """admission.enqueue_trigger's created flag: one trigger per run id."""
    created = body.run_id not in _TRIGGERS
    _TRIGGERS.add(body.run_id)
    return {"triggerId": f"t-{body.run_id}", "taskId": f"k-{body.run_id}", "created": created}


@pytest.fixture
def m():
    _TRIGGERS.clear()
    with (
        patch.object(voice_owner, "_require_room_token", lambda *a: None),
        patch.object(voice_owner.messaging_repo, "get_room", AsyncMock(return_value=META)),
        patch.object(voice_owner.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(voice_owner.messaging_repo, "member_home", lambda m, o: o),
        patch.object(voice_owner.messaging_repo, "put_voice_run", AsyncMock()) as put_run,
        patch.object(voice_owner, "start_voice_work", AsyncMock(side_effect=_start)) as sw,
    ):
        yield {"put_run": put_run, "sw": sw}


def _work(speaker="user_maya", title="Draft a post about holiday hours", brief="We close Dec 25."):
    return voice_owner.WorkIn(ownerId="org_1", roomToken="t", speakerUserId=speaker, title=title, brief=brief)


async def test_a_member_ask_starts_a_router_task_in_the_dm(m):
    out = await voice_owner.start_work(ROOM, _work())
    run_id = m["put_run"].await_args.args[1]
    assert out == {"started": True, "triggerId": f"t-{run_id}", "taskId": f"k-{run_id}"}
    run = m["put_run"].await_args.args[2]
    assert run == {"roomId": ROOM, "requesterUserId": "user_maya", "agentId": "a1"}
    body = m["sw"].await_args.args[1]
    assert (body.room_id, body.call_id, body.title) == (ROOM, ROOM, "Draft a post about holiday hours")
    assert "Asked aloud" in body.brief


async def test_the_same_ask_again_is_already_on_it(m):
    first = await voice_owner.start_work(ROOM, _work())
    out = await voice_owner.start_work(
        ROOM, _work(title=" draft a post about HOLIDAY hours ", brief="we close  dec 25.")
    )
    assert out["already"] is True and out["started"] is False and out["taskId"] == first["taskId"]


async def test_the_same_title_with_another_brief_is_another_task(m):
    a = await voice_owner.start_work(ROOM, _work(brief="We close Dec 25."))
    b = await voice_owner.start_work(ROOM, _work(brief="We open late on Dec 26."))
    assert a["started"] and b["started"] and a["taskId"] != b["taskId"]


async def test_a_failed_start_can_be_retried(m):
    m["sw"].side_effect = RuntimeError("dynamo down")
    with pytest.raises(RuntimeError):
        await voice_owner.start_work(ROOM, _work())
    m["sw"].side_effect = _start
    out = await voice_owner.start_work(ROOM, _work())
    assert out["started"] is True and "already" not in out


async def test_someone_not_seated_in_the_dm_does_nothing(m):
    with pytest.raises(HTTPException) as e:
        await voice_owner.start_work(ROOM, _work(speaker="user_other"))
    assert e.value.status_code == 403
    m["sw"].assert_not_awaited()


async def test_only_a_dm_is_owner_mode(m):
    with patch.object(voice_owner.messaging_repo, "get_room", AsyncMock(return_value={**META, "kind": "channel"})):
        with pytest.raises(HTTPException) as e:
            await voice_owner.start_work(ROOM, _work())
    assert e.value.status_code == 404


async def test_start_names_the_assistant_and_its_agent(m):
    with patch.object(voice_owner, "assistant_name", AsyncMock(return_value="Nova")):
        out = await voice_owner.start(
            ROOM, voice_owner.OwnerIn(ownerId="org_1", roomToken="t", speakerUserId="user_maya")
        )
    assert out == {"assistantName": "Nova", "agentId": "a1"}


async def test_memory_reads_with_the_members_private_scope(m):
    with patch.object(
        voice_owner.memory_api, "recall_page", AsyncMock(return_value={"results": [{"content": "Closed  Dec 25"}]})
    ) as rp:
        out = await voice_owner.read(
            ROOM,
            voice_owner.ReadIn(
                ownerId="org_1", roomToken="t", speakerUserId="user_maya", kind="memory", question="holiday hours?"
            ),
        )
    assert out == {"facts": ["Closed Dec 25"]}
    assert rp.await_args.args[:2] == ("org_1", "user_maya")


async def test_integration_read_goes_through_the_shared_reader(m):
    with patch.object(
        voice_owner.integration_reads, "read", AsyncMock(return_value="Dentist at 2026-11-09T10:00")
    ) as r:
        out = await voice_owner.integration_read(
            ROOM,
            voice_owner.IntegrationIn(
                ownerId="org_1", roomToken="t", speakerUserId="user_maya", provider="calendar", query=""
            ),
        )
    assert out == {"text": "Dentist at 2026-11-09T10:00"}
    r.assert_awaited_once_with("org_1", "calendar", "")
