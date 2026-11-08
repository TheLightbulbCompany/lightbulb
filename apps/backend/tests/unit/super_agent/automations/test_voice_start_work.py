"""Work asked aloud becomes a task (lane A, Task 10; contracts-v2 §14.1)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core import tasks_schema as ts
from core.repositories import tasks_repo

WS = "org_1"
ROOM = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
CALL = "win-1"
BODY = {"runId": RUN, "callId": CALL, "roomId": ROOM, "title": "Draft the launch mail", "brief": "Write it for Monday."}


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
    assert (first.pop("created"), second.pop("created")) == (True, False)  # owner mode's "already on it"
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


# --- a caller's work stays the caller's, at any depth, with a budget (end-state Task 4; audit H) --------------------

GROWTH = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def made(api, monkeypatch):
    """`_ensure_task` (the one place a task row is created) with its room and memory card stubbed."""
    from core.temporal import activities as acts

    monkeypatch.setattr(acts.task_conversation, "ensure_task_room", AsyncMock())
    monkeypatch.setattr(acts.memory_tasks_index, "index_task", AsyncMock())

    async def make(trigger_id: str) -> dict:
        """What the Router does with a trigger's scripted start (`ref.start`)."""
        trig = await tasks_repo.get_trigger(WS, trigger_id)
        start = acts._scripted(trig)
        return await acts._ensure_task(
            WS, ts.router_task_id(trigger_id, 0, 0), trig, "shared", "queued", title=start.title
        )

    return make


async def _voice_work(api, origin: str, run=RUN) -> str:
    from routers.internal_automations import VoiceStartIn, start_voice_work

    api.rows[run] = {"runId": run, "roomId": "call-1", "requesterUserId": "user_owner"}
    out = await start_voice_work(
        WS,
        VoiceStartIn(run_id=run, call_id="call-1", room_id="call-1", title="Find a Tuesday slot", brief="Caller asks."),
        origin=origin,
    )
    return out["triggerId"]


async def _router_child(parent: dict, trigger_id: str) -> dict:
    """A model `start_task` naming the parent, on a trigger that is not the caller's (a chat line)."""
    from core.temporal import activities as acts

    await tasks_repo.put_trigger(
        WS, {"triggerId": trigger_id, "source": "chat", "ref": {"text": "go"}, "admission": "respond",
             "lane": "shared", "actorUserId": "user_owner", "receivedAt": "2026-11-07T00:00:00Z"},
    )  # fmt: skip
    return await acts._ensure_task(
        WS, ts.new_task_id(), await tasks_repo.get_trigger(WS, trigger_id), "shared", "queued", title="child",
        specialist=GROWTH, parentTaskId=parent["taskId"],
    )  # fmt: skip


async def test_subtask_inherits_contact_origin(api, made):
    child = await _router_child(await made(await _voice_work(api, "phone_contact")), "chat:c1")
    assert child["origin"] == "phone_contact"
    assert await tasks_repo.from_contact(WS, child)  # the child's own field, not its (chat) trigger


async def test_grandchild_inherits_contact_origin(api, made):
    """Two levels: a Router sub-task, then work started from that sub-task's room through the Hermes start_work tool
    (POST /voice-start, which reaches start_voice_work with no origin of its own)."""
    child = await _router_child(await made(await _voice_work(api, "phone_contact")), "chat:c1")
    room = ts.task_room_id(child["taskId"])
    api.rows[RUN] = {"runId": RUN, "roomId": room, "agentId": "ceo-1", "requesterUserId": "user_owner"}
    api.meta.update({"roomId": room, "task": {"taskId": child["taskId"]}})
    r = _post(api, BODY | {"roomId": room, "callId": "huddle-1"})
    assert r.status_code == 202, r.text
    grandchild = await made(r.json()["triggerId"])
    assert grandchild["origin"] == "phone_contact" and await tasks_repo.from_contact(WS, grandchild)


async def test_meeting_work_is_never_a_contacts(api, made):
    """Founder ruling: everyone in a Google meeting has the inviting member's powers, so meeting work (and its
    sub-tasks) is never a contact's; only `phone_contact` is."""
    task = await made(await _voice_work(api, "meeting", run="m1"))
    child = await _router_child(task, "chat:m-child")
    for row in (task, child):
        assert row["origin"] == "meeting" and not await tasks_repo.from_contact(WS, row)


async def test_meeting_origin_grandchild_gets_confirm_all(api, made):
    """A meeting's grandchild started through /voice-start from its child's room resolves origin=meeting from that
    room's task, and so gets the meeting safeguard (every spend, send or publish is carded)."""
    child = await _router_child(await made(await _voice_work(api, "meeting", run="m1")), "chat:m-child")
    room = ts.task_room_id(child["taskId"])
    api.rows[RUN] = {"runId": RUN, "roomId": room, "agentId": "ceo-1", "requesterUserId": "user_owner"}
    api.meta.update({"roomId": room, "task": {"taskId": child["taskId"]}})
    r = _post(api, BODY | {"roomId": room, "callId": "huddle-1"})
    assert r.status_code == 202, r.text
    grandchild = await made(r.json()["triggerId"])
    assert grandchild["origin"] == "meeting" and grandchild["confirmAll"] is True
    assert not await tasks_repo.from_contact(WS, grandchild)


async def test_owner_work_is_not_contact(api, made):
    task = await made(_post(api).json()["triggerId"])
    assert not await tasks_repo.from_contact(WS, task)


async def test_contact_run_has_a_budget(monkeypatch):
    from core.temporal import activities as acts

    monkeypatch.setattr(acts, "_local_now", AsyncMock(return_value="now"))
    monkeypatch.setattr(acts.room_memory, "group_room_by_id", AsyncMock(return_value=None))
    task = {"taskId": ts.new_task_id(), "title": "x", "audience": {"kind": "room", "roomId": "r"}, "lane": "shared"}
    contact = await acts._request(WS, task | {"origin": "phone_contact"}, {"n": 1}, {"attempts": []})
    assert contact.run_budget_seconds == 600 and contact.memory["sync"] == "none"
    owner = await acts._request(WS, task | {"origin": "owner_voice"}, {"n": 1}, {"attempts": []})
    assert owner.run_budget_seconds is None  # the owner's work keeps today's limits
    assert "Connected apps" not in contact.input and "Connected apps: none" in owner.input  # never the caller's
