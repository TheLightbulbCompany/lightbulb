"""Work asked aloud becomes a task (lane A, Task 10; contracts-v2 §14.1)."""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core import tasks_schema as ts
from core.repositories import tasks_repo
from core.services import voice_calls as vc
from tests.unit.services.test_voice_huddle_calls import _dispatch, run_env  # noqa: F401 — O's harness, reused

WS = "org_1"
ROOM = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
CALL = "win-1"
BODY = {"runId": RUN, "callId": CALL, "roomId": ROOM, "title": "Draft the launch mail", "brief": "Write it for Monday."}

# origin/main's TURN_NOTE, byte for byte: the switch-off path must keep sending exactly this.
TURN_NOTE_SHA = "84cd3f1700ac67643c8ddbae91e0e82b3e4fed478917b9f947eb2e1d86ce18ec"


# --- the route ---------------------------------------------------------------------------------------------


@pytest.fixture
def api(table, monkeypatch):
    rows = {RUN: {"runId": RUN, "roomId": ROOM, "agentId": "ceo-1", "requesterUserId": "user_sam"}}
    meta = {"roomId": ROOM, "kind": "channel"}

    async def get_voice_run(owner_id, run_id):
        return rows.get(run_id)

    async def get_room(room_id):
        return meta

    with (
        patch("routers.internal_automations._require_hook_token", AsyncMock(return_value={})),
        patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=True)) as flags,
        patch("core.repositories.messaging_repo.get_voice_run", side_effect=get_voice_run),
        patch("core.repositories.messaging_repo.get_room", side_effect=get_room),
        patch("core.repositories.messaging_repo.put_admit_trigger", AsyncMock()) as outbox,
    ):
        from main import app

        yield type(
            "Api", (), {"client": TestClient(app), "rows": rows, "meta": meta, "flags": flags, "outbox": outbox}
        )()


def _post(api, body=BODY):
    return api.client.post(
        f"/api/v1/internal/automations/{WS}/voice-start", json=body, headers={"Authorization": "Bearer t"}
    )


async def test_voice_start_is_deterministic_task_in_room(api):
    r = _post(api)
    assert r.status_code == 202, r.text
    trigger_id = r.json()["triggerId"]
    assert trigger_id == f"voice:{CALL}:{RUN}"
    assert r.json()["taskId"] == ts.router_task_id(trigger_id, 0, 0)
    row = await tasks_repo.get_trigger(WS, trigger_id)
    assert row["source"] == "voice" and row["admission"] == "respond" and row["lane"] == "shared"
    assert row["actorUserId"] == "user_sam"  # the VOICERUN row's verified requester, not the model's word
    assert row["ref"]["deliverRoomId"] == ROOM and row["ref"]["agentId"] == "ceo-1"
    start = row["ref"]["start"]
    assert (
        start["kind"] == "start_task"
        and start["visibility"] == "room"
        and start["ack"] == "On it: Draft the launch mail"
    )
    assert start["title"] == "Draft the launch mail" and start["brief"] == "Write it for Monday."
    api.outbox.assert_awaited_once_with(WS, trigger_id)


async def test_same_utterance_twice_one_task(api):
    first, second = _post(api).json(), _post(api).json()
    assert first == second
    assert (await tasks_repo.get_trigger(WS, first["triggerId"]))["version"] == 1


async def test_dm_call_uses_the_requesters_private_lane(api):
    api.meta["kind"] = "dm"
    r = _post(api)
    assert (await tasks_repo.get_trigger(WS, r.json()["triggerId"]))["lane"] == ts.router_lane_key("user_sam")


async def test_a_guest_cannot_start_work(api):
    api.rows[RUN]["requesterUserId"] = None
    assert _post(api).status_code == 403
    assert await tasks_repo.get_trigger(WS, f"voice:{CALL}:{RUN}") is None


async def test_unknown_or_foreign_run_404(api):
    assert _post(api, BODY | {"runId": "nope"}).status_code == 404
    assert _post(api, BODY | {"roomId": "other-room"}).status_code == 404  # the run's room is the truth


async def test_switch_off_404(api):
    api.flags.return_value = False
    assert _post(api).status_code == 404


# --- the spoken turn ----------------------------------------------------------------------------------------


async def test_switch_off_turn_note_and_body_unchanged(run_env):  # noqa: F811
    with patch("core.services.voice_calls.flags_for", AsyncMock(return_value=False)):
        await _dispatch()
    req = run_env.await_args.kwargs["json_body"]["request"]
    assert req["instructions"] == vc.TURN_NOTE
    assert hashlib.sha256(vc.TURN_NOTE.encode()).hexdigest() == TURN_NOTE_SHA
    assert "superAgent" not in req


async def test_switch_on_adds_one_sentence_and_the_flag(run_env):  # noqa: F811
    with patch("core.services.voice_calls.flags_for", AsyncMock(return_value=True)):
        await _dispatch()
    req = run_env.await_args.kwargs["json_body"]["request"]
    assert req["superAgent"] is True
    assert req["instructions"] == vc.TURN_NOTE + "\n" + vc.SUPER_AGENT_NOTE
    assert "start_work" in vc.SUPER_AGENT_NOTE and "paperclip" not in vc.SUPER_AGENT_NOTE.lower()
    assert vc.SUPER_AGENT_NOTE.count(". ") <= 2  # one sentence, two at most
