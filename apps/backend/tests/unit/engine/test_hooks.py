"""The engine's three calls into the products (`core.engine.hooks`), and the Temporal and admission sites that make
them (engine-service plan, Task 1). The product side is `core.voice.engine_hooks`, registered for every test by
`tests/conftest.py`; `seen` wraps it to record what the engine asked and said."""

import uuid
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from core.engine import hooks
from core.engine.hooks import ContextAnswer, ContextAsk
from core.repositories import tasks_repo
from core.services import admission
from core.temporal import activities as acts
from core.temporal.types import RouterTurnIn, WorkerTurnIn

from ..super_agent.orchestration import test_activities_task as task_world
from ..super_agent.orchestration import test_admission as admit_world
from ..super_agent.orchestration.test_tasks_repo import table  # noqa: F401

WS = "org_1"
ROOM = str(uuid.uuid4())
NOTES = f"meeting-notes:{ROOM}:1"


@pytest.fixture
def seen(monkeypatch):
    """The real product hooks, with every context ask and event recorded; `answers[purpose]` overrides an answer."""
    from core.voice import engine_hooks

    log: dict = {"asks": [], "events": [], "answers": {}}

    async def context(ask: ContextAsk) -> ContextAnswer:
        log["asks"].append(ask)
        if ask.purpose in log["answers"]:
            return log["answers"][ask.purpose]
        return await engine_hooks._context(ask)

    async def event(e) -> None:
        log["events"].append(e)
        await engine_hooks._event(e)

    monkeypatch.setattr(hooks, "_registered", None)
    hooks.register(tool=engine_hooks._tool, context=context, event=event)
    return log


async def test_unregistered_hook_raises(monkeypatch):
    monkeypatch.setattr(hooks, "_registered", None)
    with pytest.raises(hooks.HookNotRegistered):
        await hooks.context(ContextAsk(tenant_id=WS, purpose="router"))
    with pytest.raises(hooks.HookNotRegistered):
        await hooks.event(hooks.EngineEvent(type="task.moved", tenant_id=WS))


@pytest.fixture
def worker(table, monkeypatch):  # noqa: F811
    return task_world.world.__wrapped__(table, monkeypatch)


async def test_transcript_missing_skips_worker_run(seen, worker, monkeypatch):
    """A `worker` context with allow=False is TranscriptUnavailable's path: no run, the reason posted, `failed`."""
    from core.services import room_memory

    monkeypatch.setattr(room_memory, "group_room_by_id", AsyncMock(return_value=None))
    seen["answers"]["worker"] = ContextAnswer(allow=False, text="No transcript to write notes from.")
    tid = await task_world._task(triggerId=NOTES)
    ref = await task_world._attempt(tid)
    out = await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert (out.outcome, out.error) == ("failed", "TranscriptUnavailable")
    assert worker["runs"] == []
    assert any(p["text"] == "No transcript to write notes from." for p in worker["posts"])
    assert [a.purpose for a in seen["asks"]] == ["worker"] and seen["asks"][0].task["taskId"] == tid


@pytest.mark.parametrize(
    "to,reason,records", [("idle", None, True), ("resolved", "stopped", False), ("failed", None, True)]
)
async def test_task_moved_reaches_meeting_notes_and_records(seen, monkeypatch, to, reason, records):
    """One `task.moved` event: the notes keep hears every end (a person's Stop too); the meeting's follow-up record
    only the moves that get a lifecycle line, as before."""
    from core.voice.meetings import meeting_notes, records as meeting_records

    task = {"taskId": "t1", "status": to, "triggerId": NOTES}
    monkeypatch.setattr(tasks_repo, "get_task_tree", AsyncMock(return_value={"task": task, "attempts": []}))
    keep, follow = AsyncMock(), AsyncMock()
    monkeypatch.setattr(meeting_notes, "on_task_moved", keep)
    monkeypatch.setattr(meeting_records, "task_moved", follow)
    await acts._announce(WS, "t1", to, reason)
    keep.assert_awaited_once_with(task, to)
    assert follow.await_count == (1 if records else 0)
    [e] = seen["events"]
    assert (e.type, e.to, e.task, e.announced) == ("task.moved", to, task, records)


@pytest.fixture
def router(table, monkeypatch):  # noqa: F811
    from ..super_agent.orchestration import test_router_turn

    return test_router_turn.world.__wrapped__(table, monkeypatch)


async def test_dropped_notes_trigger_marks_no_notes(seen, router, monkeypatch):
    """Every place a trigger starts no task emits `trigger.dropped`; meeting_notes marks the meeting "no notes"."""
    from core.voice.meetings import meeting_notes

    marked = AsyncMock()
    monkeypatch.setattr(meeting_notes, "on_not_started", marked)
    trig = {
        "triggerId": NOTES,
        "source": "webhook",
        "admission": "respond",
        "lane": "shared",
        "ref": {"deliverRoomId": ROOM, "meetingNotes": {"requestId": NOTES}},
    }
    paused = AsyncMock(return_value={"pausedAt": 1})
    monkeypatch.setattr(admission, "paused_room", paused)
    monkeypatch.setattr(tasks_repo, "get_trigger", AsyncMock(return_value=trig))
    monkeypatch.setattr(acts, "_deliver", AsyncMock(return_value=None))
    monkeypatch.setattr(acts, "_read_quietly", AsyncMock())

    await admission._admit_trigger(WS, NOTES)  # 1. admission: a paused output room
    await acts._drop_paused(WS, {NOTES: dict(trig)})  # 2. the Router: paused since admission
    paused.return_value = None
    await acts.apply_fallback(WS, [NOTES])  # 3. the Router failed past its retries
    router["gate"] = {"blocked": True, "message": "Out of credits."}
    start = acts.ts.StartTask(kind="start_task", trigger_ids=[NOTES], title="Notes", brief="Notes", ack="On it.")
    await acts._apply(WS, RouterTurnIn(WS, "shared", "b1", [NOTES]), {NOTES: trig}, [start], None)  # 4. credit gate

    assert [e.type for e in seen["events"]] == ["trigger.dropped"] * 4
    assert all(e.trigger["triggerId"] == NOTES for e in seen["events"])
    assert marked.await_count == 4


@pytest.fixture
def admitting(table, monkeypatch):  # noqa: F811
    return admit_world.world.__wrapped__(table, monkeypatch)


async def test_admit_asks_context_only_for_annotated_rooms(seen, admitting):
    """A plain channel's message asks the product nothing; a meeting room's asks once and keeps its flags."""
    await admit_world._admit(admitting, admit_world._msg(1))
    assert seen["asks"] == []
    admitting["meta"] = {**admitting["meta"], "meeting": {"status": "in_call", "recallBotId": "bot", "inCallAt": 1}}
    msg = {**admit_world._msg(2), "via": "meeting_chat"}
    tid = await admit_world._admit(admitting, msg)
    [ask] = seen["asks"]
    assert (ask.purpose, ask.conversation_id, ask.annotations) == (
        "admit",
        admit_world.ROOM,
        {"meeting": admitting["meta"]["meeting"]},
    )
    trig = await tasks_repo.get_trigger(admit_world.WS, tid)
    assert trig["admission"] == "candidate"
    assert (trig["ref"]["origin"], trig["ref"]["meetingBotId"], trig["ref"]["meetingInCallAt"]) == ("meeting", "bot", 1)


async def test_unauthorized_followup_dropped(seen, monkeypatch):
    """A meeting follow-up the product no longer authorizes: admission drops it; the Router raises NotAuthorized."""
    from core.voice.meetings import meeting_context

    trig = {
        "triggerId": "meeting-followups:r:1",
        "source": "webhook",
        "admission": "candidate",
        "lane": "shared",
        "ref": {"roomId": ROOM, "meetingFollowups": {"requestId": "notes:1"}},
    }
    monkeypatch.setattr(tasks_repo, "get_trigger", AsyncMock(return_value=trig))
    monkeypatch.setattr(meeting_context, "authorize", AsyncMock(side_effect=meeting_context.NotAuthorized("revoked")))
    signal = AsyncMock()
    monkeypatch.setattr(admission, "_signal", signal)
    await admission._admit_trigger(WS, trig["triggerId"])
    signal.assert_not_awaited()
    assert [(a.purpose, a.trigger["triggerId"]) for a in seen["asks"]] == [("authorize", trig["triggerId"])]
    with pytest.raises(meeting_context.NotAuthorized, match="revoked"):
        await acts._ensure_task(WS, "t1", trig, "shared", "queued")


async def test_answer_emits_question_answered_once_on_replay(seen, monkeypatch):
    """The same press again on an answered call Question finishes what a failed first press left: one
    `question.answered`, before the view, and the product keeps the answer and resolves the call."""
    from core.voice.phone import calls
    from routers import questions as qr

    q = {
        "qid": "q1",
        "state": "answered",
        "clientAnswerId": "c1",
        "answeredBy": "user_sam",
        "subkind": "didnt_know",
        "title": "Answer Priya: Parking?",
        "preview": {"kind": "didnt_know", "roomId": "call-1", "question": "Parking?"},
    }
    rows = {"q": q}
    monkeypatch.setattr(qr.tasks_repo, "get_task", AsyncMock(return_value={"taskId": "t1"}))
    monkeypatch.setattr(qr.tasks_access, "can_view", AsyncMock(return_value=True))
    monkeypatch.setattr(qr.questions_repo, "get_question", AsyncMock(side_effect=lambda *a: rows["q"]))
    from core.engine import api

    monkeypatch.setattr(api, "_answered_view", AsyncMock(return_value={"question": {}}))
    remember, resolve = AsyncMock(), AsyncMock()
    monkeypatch.setattr(calls, "remember_answer", remember)
    monkeypatch.setattr(calls, "resolve_call", resolve)
    body = qr.AnswerBody(option_id="answer", text="Yes, out back.", client_answer_id="c1")

    await qr.submit_answer(WS, "t1", "q1", "user_sam", body)

    [e] = seen["events"]
    assert (e.type, e.tenant_id, e.question, e.actor_user_id) == ("question.answered", WS, q, "user_sam")
    assert e.answer == {"optionId": "answer", "text": "Yes, out back."}
    remember.assert_awaited_once_with(WS, "Parking?", "Yes, out back.", "user_sam")
    resolve.assert_awaited_once_with(WS, "call-1", "user_sam")

    rows["q"] = {**q, "subkind": "purchase", "preview": {"kind": "purchase", "item": "acme.dev"}}
    await qr.submit_answer(WS, "t1", "q1", "user_sam", body)
    assert len(seen["events"]) == 1  # a Question that names no conversation tells the products nothing
