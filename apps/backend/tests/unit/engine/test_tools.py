"""The agent runtime's product tool routes (engine-service plan, Task 3): each keeps its path, request model and
checks, then hands the call to `core.engine.hooks.tool`. The `*_bodies` tests pin each route's exact status codes and
JSON bodies (written against the routes before the move, unchanged after it)."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from core.repositories import tasks_repo as repo
from routers import runtime_internal as ri

from ..super_agent.orchestration.test_activities_task import WS, _task, world  # noqa: F401
from ..super_agent.orchestration.test_runtime_internal_routes import H, TOKEN, _running, _secret
from ..super_agent.orchestration.test_tasks_repo import table  # noqa: F401

CALL = "11111111-2222-3333-4444-555555555555"
BASE = f"/api/v1/internal/runtime/{WS}"
BAD = {"Authorization": "Bearer wrong"}


@pytest.fixture
def api(world, monkeypatch):  # noqa: F811
    async def container(owner_id, **_):
        return {"hook_token": TOKEN} if owner_id == WS else None

    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", container)
    from routers import messaging as messaging_router

    app = FastAPI()
    app.include_router(ri.router, prefix="/api/v1/internal/runtime")
    app.include_router(messaging_router.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _caller_run(monkeypatch, **ref):
    tid = await _task(origin="phone_contact", triggerId="voice:c1:r1")
    trig = {"voice:c1:r1": {"triggerId": "voice:c1:r1", "ref": {"origin": "phone_contact", "callId": CALL, **ref}}}
    monkeypatch.setattr(repo, "get_trigger", AsyncMock(side_effect=lambda _ws, t: trig.get(t)))
    ref_ = await _running(tid)
    return {"taskId": tid, "runId": ref_.run_id, "attempt": 1, "runSecret": await _secret(tid)}


def _json(r):
    return r.status_code, r.json()


# --- characterization: exact status and body per route ------------------------------------------------------------


async def test_front_desk_route_bodies(api, monkeypatch):
    from routers import voice_phone as vp

    monkeypatch.setattr(vp.messaging_repo, "get_room", AsyncMock(return_value={"kind": "call", "ownerId": WS}))
    monkeypatch.setattr(vp, "book", AsyncMock(return_value={"booked": True, "start": "2026-11-14T14:00"}))
    run = await _caller_run(monkeypatch)
    body = {**run, "action": "book", "start": "2026-11-14T14:00", "service": "Cut", "name": "Ana"}
    url = f"{BASE}/front-desk"
    assert _json(await api.post(url, json=body, headers=H)) == (200, {"booked": True, "start": "2026-11-14T14:00"})
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    bad = {**body, "runSecret": "x"}
    assert _json(await api.post(url, json=bad, headers=H)) == (403, {"detail": "run_not_in_workspace"})
    assert _json(await api.post(url, json={**body, "start": "Saturday"}, headers=H)) == (
        422,
        {"detail": "invalid_arguments"},
    )
    monkeypatch.setattr(vp.messaging_repo, "get_room", AsyncMock(return_value={"kind": "call", "ownerId": "org_x"}))
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": "not_a_caller_thread"})
    member = await _task()
    mref = await _running(member)
    mine = {**body, "taskId": member, "runId": mref.run_id, "runSecret": await _secret(member)}
    assert _json(await api.post(url, json=mine, headers=H)) == (403, {"detail": "not_a_caller_thread"})
    await repo.update_attempt(WS, member, 1, {"status": "ended"}, allowed_status=frozenset({"running"}))
    assert _json(await api.post(url, json=mine, headers=H)) == (409, {"detail": "stale_run"})


async def test_business_facts_route_bodies(api, monkeypatch):
    from core.voice.phone import business_profile

    monkeypatch.setattr(business_profile, "facts_text", AsyncMock(return_value="Cut $40"))
    tid = await _task()
    ref = await _running(tid)
    body = {"taskId": tid, "runId": ref.run_id, "attempt": 1, "runSecret": await _secret(tid)}
    url = f"{BASE}/business-facts"
    assert _json(await api.post(url, json=body, headers=H)) == (200, {"facts": "Cut $40"})
    assert _json(await api.post(url, json={**body, "runSecret": "x"}, headers=H)) == (
        403,
        {"detail": "run_not_in_workspace"},
    )
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})


async def test_transfer_call_route_bodies(api, monkeypatch):
    from routers import voice_phone as vp

    rooms = {CALL: {"kind": "call", "ownerId": WS, "call": {}}}
    monkeypatch.setattr(vp.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))
    monkeypatch.setattr(vp, "agent_config", AsyncMock(return_value={}))
    state = {"line": "live", "to": "+14155550199"}
    monkeypatch.setattr(vp, "_line_state", lambda cfg, caller: state["line"])
    monkeypatch.setattr(vp, "handoff_to", lambda cfg: state["to"])
    patched = AsyncMock()
    monkeypatch.setattr(vp, "_patch_call", patched)
    trig = {"voice:c1:t0": {"triggerId": "voice:c1:t0", "routedTo": {"automationId": "a1"},
                            "ref": {"origin": "phone_contact", "callId": CALL, "roomId": CALL}}}  # fmt: skip
    monkeypatch.setattr(repo, "get_trigger", AsyncMock(side_effect=lambda _ws, t: trig.get(t)))
    routed = AsyncMock()
    monkeypatch.setattr(repo, "set_trigger_routed_to", routed)
    posted = AsyncMock(return_value="msg-1")
    monkeypatch.setattr(ri.task_conversation, "post_as_lightbulb", posted)
    url = f"{BASE}/transfer-call"
    body = {"triggerIds": ["voice:c1:t0"], "text": "Putting you through."}
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    assert _json(await api.post(url, json=body, headers=H)) == (200, {"messageId": "msg-1"})
    assert routed.await_args.args == (WS, "voice:c1:t0", {"automationId": "a1", "transfer": "msg-1"})
    assert posted.await_args.kwargs["event_id"] == "call-transfer:voice:c1:t0"
    assert _json(await api.post(url, json={**body, "triggerIds": ["nope"]}, headers=H)) == (
        403,
        {"detail": "not_a_caller"},
    )
    state["to"] = None
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": {"code": "nobody_available"}})
    assert patched.await_count == 1 and posted.await_count == 1 and routed.await_count == 1


async def test_meeting_context_route_bodies(api, monkeypatch):
    from core.engine.contract import GRANT_HEADER
    from core.engine.hooks import NotAuthorized
    from core.voice.meetings import meeting_context

    page = {"quotedSource": True, "text": "page", "nextCursor": None}
    source = AsyncMock(return_value=page)
    monkeypatch.setattr(meeting_context, "context", source)
    url = f"{BASE}/meeting-context"
    body = {"runId": "run-1", "triggerId": "meeting-followups:r:1", "section": "tasks", "cursor": "c1"}
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    assert _json(await api.post(url, json=body, headers={**H, GRANT_HEADER: "g"})) == (200, page)
    source.assert_awaited_once_with(WS, "run-1", "meeting-followups:r:1", "tasks", "c1", grant="g")
    source.side_effect = NotAuthorized("not_in_this_run")
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": "not_in_this_run"})
    source.side_effect = ValueError("bad cursor")
    assert _json(await api.post(url, json=body, headers=H)) == (422, {"detail": "invalid_arguments"})


async def test_meeting_chat_route_bodies(api, monkeypatch):
    from core.voice.meetings import meeting_chat, meeting_invites

    sent = AsyncMock(return_value={"status": "refused", "replayed": True})
    monkeypatch.setattr(meeting_chat, "send", sent)
    url = f"{BASE}/meeting-chat"
    body = {"triggerIds": ["chat:m"], "text": "The link"}
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    assert _json(await api.post(url, json=body, headers=H)) == (200, {"status": "refused", "replayed": True})
    sent.side_effect = meeting_chat.NotCurrentMeeting("not_current_meeting")
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": "not_current_meeting"})
    sent.side_effect = meeting_invites.MeetingBusy()
    assert _json(await api.post(url, json=body, headers=H)) == (503, {"detail": "meeting_busy"})
    sent.side_effect = ValueError("too long")
    assert _json(await api.post(url, json=body, headers=H)) == (422, {"detail": "invalid_arguments"})


async def test_meeting_stop_transcribing_route_bodies(api, monkeypatch):
    from core.voice.meetings import meeting_chat, meeting_invites

    out = {"notTranscribed": [{"id": "12", "name": "Sam"}]}
    stop = AsyncMock(return_value=out)
    monkeypatch.setattr(meeting_chat, "stop_transcribing", stop)
    url = f"{BASE}/meeting-stop-transcribing"
    body = {"triggerIds": ["chat:m"], "participantId": "12"}
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    assert _json(await api.post(url, json=body, headers=H)) == (200, out)
    stop.assert_awaited_once_with(WS, ["chat:m"], "12")
    stop.side_effect = meeting_chat.NotCurrentMeeting("not_the_speaker")
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": "not_the_speaker"})
    stop.side_effect = meeting_invites.MeetingBusy()
    assert _json(await api.post(url, json=body, headers=H)) == (503, {"detail": "meeting_busy"})


async def test_voice_profile_route_bodies(api, monkeypatch):
    from core.voice import voice_calls

    edited = {"ok": True, "agentId": "a1", "name": "Nova", "notes": ""}
    edit = AsyncMock(return_value=edited)
    monkeypatch.setattr(voice_calls, "edit_voice_profile", edit)
    url = f"/internal/agents/voice-profile/{WS}"
    body = {"agentId": "a1", "runId": "run-1", "action": "rename", "name": "Nova"}
    assert _json(await api.post(url, json=body, headers=BAD)) == (401, {"detail": "invalid hook token"})
    assert _json(await api.post(url, json=body, headers=H)) == (200, edited)
    edit.assert_awaited_once_with(WS, agent_id="a1", run_id="run-1", action="rename", name="Nova", text=None)
    edit.side_effect = HTTPException(status_code=403, detail={"code": "not_a_member"})
    assert _json(await api.post(url, json=body, headers=H)) == (403, {"detail": {"code": "not_a_member"}})
    r = await api.post(url, json={**body, "action": "add_note", "text": "x" * 301}, headers=H)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "text"]
    assert edit.await_count == 2


# --- the hook ---------------------------------------------------------------------------------------------------------

TOOLS = [
    "front_desk",
    "business_facts",
    "transfer_call",
    "meeting_context",
    "meeting_chat",
    "meeting_stop_transcribing",
    "voice_profile",
]


def _with(monkeypatch, *, tool=None, context=None):
    """The registered product hooks, with `tool` and/or `context` swapped."""
    from core.engine import hooks

    t, c, e = hooks._hooks()
    monkeypatch.setattr(hooks, "_registered", (tool or t, context or c, e))


async def _case(name, monkeypatch):
    """(url, a request that passes the route's checks, the run it speaks for or None)."""
    if name == "front_desk":
        run = await _caller_run(monkeypatch)
        return f"{BASE}/front-desk", {**run, "action": "my_bookings"}, run
    if name == "business_facts":
        tid = await _task()
        ref = await _running(tid)
        run = {"taskId": tid, "runId": ref.run_id, "attempt": 1, "runSecret": await _secret(tid)}
        return f"{BASE}/business-facts", run, run
    if name == "transfer_call":
        monkeypatch.setattr(repo, "get_trigger", AsyncMock(return_value={"triggerId": "voice:c1:t0", "ref": {}}))
        return f"{BASE}/transfer-call", {"triggerIds": ["voice:c1:t0"], "text": "One moment."}, None
    if name == "meeting_context":
        return f"{BASE}/meeting-context", {"runId": "run-1", "triggerId": "mf:1", "section": "transcript"}, None
    if name == "meeting_chat":
        return f"{BASE}/meeting-chat", {"triggerIds": ["chat:m"], "text": "The link"}, None
    if name == "meeting_stop_transcribing":
        return f"{BASE}/meeting-stop-transcribing", {"triggerIds": ["chat:m"]}, None
    return f"/internal/agents/voice-profile/{WS}", {"agentId": "a1", "runId": "run-1", "action": "clear_notes"}, None


@pytest.mark.parametrize("name", TOOLS)
async def test_each_tool_route_calls_hook_with_checked_run(api, monkeypatch, name):
    calls = []

    async def tool(n, call):
        calls.append((n, call))
        return {"tool": n}

    _with(monkeypatch, tool=tool)
    url, body, run = await _case(name, monkeypatch)
    assert (await api.post(url, json=body, headers=BAD)).status_code == 401
    if run:  # a bad run secret, or a run that is not the task's live attempt, never reaches the product
        assert (await api.post(url, json={**body, "runSecret": "x"}, headers=H)).status_code == 403
        await repo.update_attempt(WS, run["taskId"], 1, {"status": "ended"}, allowed_status=frozenset({"running"}))
        if name == "front_desk":
            assert (await api.post(url, json=body, headers=H)).status_code == 409
        await repo.update_attempt(WS, run["taskId"], 1, {"status": "running"}, allowed_status=frozenset({"ended"}))
    assert calls == []
    assert _json(await api.post(url, json=body, headers=H)) == (200, {"tool": name})
    ((called, call),) = calls
    assert called == name and call.tenant_id == WS
    assert not {"runSecret", "run_secret"} & call.args.keys()  # the run's secret stays in the engine
    if run:
        assert (call.task_id, call.run_id) == (run["taskId"], run["runId"])


async def test_meeting_chat_routed_to_written_by_engine(api, monkeypatch):
    """The product answers which key its line fills; the engine merges it into each trigger's `routedTo`."""

    async def tool(n, call):
        return {"status": "sent", "replayed": False, "routedTo": {"meetingChat": "m1"}}

    _with(monkeypatch, tool=tool)
    trig = {"chat:a": {"triggerId": "chat:a", "routedTo": {"automationId": "x"}}, "chat:b": {"triggerId": "chat:b"}}
    monkeypatch.setattr(repo, "get_trigger", AsyncMock(side_effect=lambda _ws, t: trig.get(t)))
    routed = AsyncMock()
    monkeypatch.setattr(repo, "set_trigger_routed_to", routed)
    r = await api.post(f"{BASE}/meeting-chat", json={"triggerIds": ["chat:a", "chat:b"], "text": "The link"}, headers=H)
    assert _json(r) == (200, {"status": "sent", "replayed": False})
    assert [c.args for c in routed.await_args_list] == [
        (WS, "chat:a", {"automationId": "x", "meetingChat": "m1"}),
        (WS, "chat:b", {"meetingChat": "m1"}),
    ]


async def test_voice_start_requester_from_context(monkeypatch):
    """voice-start's requester, agent and room come from the product (its VOICERUN row), never the engine's read."""
    from core import tasks_schema as ts
    from core.engine.hooks import ContextAnswer
    from core.repositories import messaging_repo
    from core.services import admission
    from routers.internal_automations import VoiceStartIn, start_voice_work

    asks = []
    answer = ContextAnswer(conversation_id="room-1", requester_user_id="user_sam", agent_id="ceo-1")

    async def context(ask):
        asks.append(ask)
        return answer

    _with(monkeypatch, context=context)
    monkeypatch.setattr(messaging_repo, "get_voice_run", AsyncMock(side_effect=AssertionError("engine read")))
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"kind": "dm"}))
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(admission, "enqueue_trigger", enqueue)
    body = VoiceStartIn(run_id="run-1", call_id="c1", room_id="room-1", title="Mail", brief="Write it.")
    out = await start_voice_work(WS, body)
    assert [(a.purpose, a.tenant_id, a.run_id) for a in asks] == [("voice_run", WS, "run-1")]
    trigger = enqueue.await_args.args[1]
    assert trigger.actor_user_id == "user_sam" and trigger.lane == ts.router_lane_key("user_sam")
    assert trigger.ref["agentId"] == "ceo-1" and out["created"] is True
    for wrong, status, code in (
        (ContextAnswer(), 404, "unknown_run"),
        (ContextAnswer(conversation_id="room-2", requester_user_id="user_sam"), 404, "unknown_run"),
        (ContextAnswer(conversation_id="room-1"), 403, "no_requester"),
    ):
        answer = wrong
        with pytest.raises(HTTPException) as exc:
            await start_voice_work(WS, body)
        assert (exc.value.status_code, exc.value.detail) == (status, {"code": code})
    assert enqueue.await_count == 1


async def test_meeting_chat_skips_a_trigger_gone_on_the_re_read(api, monkeypatch):
    """The line was already sent: a trigger missing on the engine's re-read is skipped, never a 500."""

    async def tool(n, call):
        return {"status": "sent", "replayed": True, "routedTo": {"meetingChat": "m1"}}

    _with(monkeypatch, tool=tool)
    trig = {"chat:a": {"triggerId": "chat:a"}}
    monkeypatch.setattr(repo, "get_trigger", AsyncMock(side_effect=lambda _ws, t: trig.get(t)))
    routed = AsyncMock()
    monkeypatch.setattr(repo, "set_trigger_routed_to", routed)
    r = await api.post(f"{BASE}/meeting-chat", json={"triggerIds": ["gone", "chat:a"], "text": "The link"}, headers=H)
    assert _json(r) == (200, {"status": "sent", "replayed": True})
    assert [c.args for c in routed.await_args_list] == [(WS, "chat:a", {"meetingChat": "m1"})]
