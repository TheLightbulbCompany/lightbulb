"""A meeting keeps its transcript and notes in the app; completion never automatically emails anyone."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import ses_email
from core.voice.meetings import meeting_notes as mod

ROOM = "11111111-1111-4111-8111-111111111111"
CEO = "22222222-2222-4222-8222-222222222222"
MEET = "https://meet.google.com/abc-defg-hij"
DEFAULT = mod.messaging.default_room_id("org_1")  # the output room: every line lands there
TRIG = f"meeting-notes:{ROOM}:1790972400"  # the notes task's trigger, and its request id
TASK = mod.ts.router_task_id(TRIG, 0, 0)


@pytest.fixture
def env(monkeypatch):
    meta = {
        "roomId": ROOM,
        "ownerId": "org_1",
        "meeting": {
            "title": "Weekly sync",
            "organizer": "pat@acme.com",
            "meetingUrl": MEET,
            "status": "ended",
            "startAt": 1790972400,
            "inCallAt": 1790972500,
            "endedAt": 1790976100,
        },
    }

    async def patch_meeting(room_id, fields, drop=(), when=None):
        if m.patch_failures:  # a transient DynamoDB failure on the next N writes
            m.patch_failures -= 1
            raise RuntimeError("ddb")
        if when is not None and not when(meta["meeting"]):
            return None
        meta["meeting"] = {**meta["meeting"], **(fields(meta["meeting"]) if callable(fields) else fields)}
        return meta["meeting"]

    attachment = SimpleNamespace(id="att-1", model_dump=lambda **_: {"id": "att-1", "fileName": "transcript.txt"})
    m = SimpleNamespace(
        meta=meta,
        store=MagicMock(return_value=attachment),
        line=AsyncMock(return_value={"messageId": "msg-1", "messageSeq": 7}),
        send=AsyncMock(return_value="<id@ses>"),
        sleep=AsyncMock(),
        claim=AsyncMock(return_value="tok"),
        release=AsyncMock(),
        members=AsyncMock(return_value=[{"kind": "human", "id": "user_1"}, {"kind": "agent", "id": CEO}]),
        front=AsyncMock(return_value=CEO),
        enqueue=AsyncMock(return_value=True),
        attempts=[],
        patch_failures=0,
        advance=AsyncMock(),
        metric=MagicMock(),
        requests={},
        posted=[],
    )

    async def put_request(owner_id, message_id, fields):
        m.requests[message_id] = dict(fields)

    async def claim_mail(owner_id, message_id):
        request = m.requests.get(message_id)
        if request is None or request.get("mailAttemptedAt"):
            return False
        request["mailAttemptedAt"] = 1
        return True

    monkeypatch.setattr(mod.messaging_repo, "put_meeting_notes_request", put_request)
    monkeypatch.setattr(mod.messaging_repo, "claim_meeting_notes_mail", claim_mail)
    monkeypatch.setattr(mod.messaging_repo, "list_messages", AsyncMock(side_effect=lambda *_a: (m.posted, None)))
    monkeypatch.setattr(
        mod.messaging_repo, "get_meeting_notes_request", AsyncMock(side_effect=lambda _o, mid: m.requests.get(mid))
    )
    monkeypatch.setattr(mod.messaging_repo, "list_members", m.members)
    monkeypatch.setattr(mod.task_conversation, "front_agent_id", m.front)
    monkeypatch.setattr(mod.admission, "enqueue_trigger", m.enqueue)
    monkeypatch.setattr(
        mod.tasks_repo, "get_task_tree", AsyncMock(side_effect=lambda _w, t: {"task": {}, "attempts": m.attempts})
    )
    monkeypatch.setattr(mod.meeting_invites, "advance", m.advance)
    monkeypatch.setattr(mod, "put_metric", m.metric)
    monkeypatch.setattr(mod.container_repo, "release_owner_lifecycle_claim", m.release)
    monkeypatch.setattr(mod.container_repo, "try_claim_owner_lifecycle", m.claim)
    monkeypatch.setattr(mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta))
    monkeypatch.setattr(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting))
    m.meeting_tasks = AsyncMock(return_value=[])
    monkeypatch.setattr(mod, "_meeting_tasks", m.meeting_tasks)
    monkeypatch.setattr(mod.meeting_invites, "_post_line", m.line)
    monkeypatch.setattr(mod.messaging, "store_attachment", m.store)
    head = MagicMock(return_value=(SimpleNamespace(size=100), b"["))  # a short transcript: nothing cut
    monkeypatch.setattr(mod.messaging, "load_attachment_head", head)
    monkeypatch.setattr(ses_email, "send_ses_email", m.send)
    monkeypatch.setattr(mod.asyncio, "sleep", m.sleep)
    return m


NOTES = "Decisions: ship the beta Friday. Open: pricing. Tasks filed: ISOA-1 (Pat), ISOA-2 (Ada)."


def notes_task(**fields):
    return {"pk": "WS#org_1", "taskId": TASK, "triggerId": TRIG, **fields}


async def task_done(env, notes=NOTES, to="idle"):
    """The notes task reaches a status (activities._announce): `idle` with its attempt's output."""
    env.attempts[:] = [{"n": 1, "outcome": "completed", "output": notes}] if to == "idle" else []
    await mod.on_task_moved(notes_task(status=to), to)


async def ended_and_noted(env, notes=NOTES):
    """The call ends, then the notes task finishes with ``notes`` as its output."""
    await mod.on_ended(ROOM)
    await task_done(env, notes)


def started(env):
    """The StartTask the notes trigger carries (the one enqueued trigger)."""
    trigger = env.enqueue.await_args.args[1]
    return trigger, trigger.ref["start"]


def _line(mark, name, text, at):
    return {"mark": mark, "kind": "person", "name": name, "text": text, "at": at, "to": ""}


async def test_a_final_transcript_attaches_once(env):
    await mod.on_transcript(ROOM, [_line(0, "Pat", "Hi all", 1790972595.0)], False)
    env.store.assert_not_called()  # a rolling flush is only logged
    whole = [_line(1, "Ada", "Hello", 1790972655.0), _line(0, "Pat", "Hi all", 1790972595.0)]
    await mod.on_transcript(ROOM, whole, True)
    env.store.assert_called_once()
    owner, room, name, ctype, data = env.store.call_args.args
    assert (owner, room, name, ctype) == ("org_1", DEFAULT, "transcript.txt", "text/plain")
    assert data.decode() == "[20:23] Pat: Hi all\n[20:24] Ada: Hello\n"  # UTC, by mark
    env.line.assert_awaited_once()
    assert env.line.await_args.args[1:] == (
        DEFAULT,
        f"meeting:{ROOM}:1790972400:transcript",
        "Transcript of **Weekly sync** attached.",
    )
    assert env.line.await_args.kwargs["attachments"] == [{"id": "att-1", "fileName": "transcript.txt"}]
    assert env.meta["meeting"]["transcriptAttachedAt"]
    assert (env.meta["meeting"]["transcriptId"], env.meta["meeting"]["transcriptRoomId"]) == ("att-1", DEFAULT)
    await mod.on_transcript(ROOM, whole, True)  # a retried final
    env.store.assert_called_once()


async def test_an_empty_final_attaches_nothing(env):
    await mod.on_transcript(ROOM, [], True)
    env.store.assert_not_called()


async def test_meeting_end_enqueues_notes_task(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_ended(ROOM)
    env.enqueue.assert_awaited_once()
    env.line.assert_not_awaited()  # no dispatch_targets line: the Router's task path writes the notes
    owner, trigger = env.enqueue.await_args.args
    assert (owner, env.enqueue.await_args.kwargs) == ("org_1", {"room_id": DEFAULT})
    assert (trigger.trigger_id, trigger.source, trigger.admission, trigger.lane) == (
        TRIG,
        "webhook",
        "respond",
        "shared",
    )
    assert trigger.ref["meetingNotes"] == {"requestId": TRIG} and trigger.ref["taskId"] == TASK
    assert set(trigger.ref) == {"taskId", "meetingNotes", "start", "confirmAll"}  # the transcript by reference only
    assert trigger.ref["confirmAll"] is True  # transcript lines are anyone's words: every outside action gets a card
    start = mod.ts.StartTask.model_validate({**trigger.ref["start"], "triggerIds": [TRIG]})
    assert start.specialist == CEO and start.visibility == "room"
    assert start.brief.startswith("Write the meeting notes for Weekly sync: open questions")
    assert start.brief.endswith("The transcript is below")
    env.front.assert_awaited_with("org_1", room_id=DEFAULT)
    assert env.meta["meeting"]["notesRunId"] == TASK
    assert env.requests[TRIG] == {
        "roomId": ROOM, "occurrence": 1790972400, "targetRoomId": DEFAULT, "agentId": CEO, "taskId": TASK,
        "meeting": {"title": "Weekly sync", "organizer": "pat@acme.com", "transcriptAttachedAt": 1, "inCallAt": 1790972500, "endedAt": 1790976100},
    }  # fmt: skip
    env.sleep.assert_not_awaited()


async def test_meeting_notes_not_emailed(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notes"] == NOTES.strip()  # the app's copy, for the timeline


async def test_on_ended_waits_for_the_transcript_then_proceeds(env):
    async def attach_on_second(_s):
        if env.sleep.await_count == 2:
            env.meta["meeting"]["transcriptAttachedAt"] = 1

    env.sleep.side_effect = attach_on_second
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 2
    env.enqueue.assert_awaited_once()


async def test_meeting_end_without_transcript_marks_no_notes(env):
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 30 and env.sleep.await_args.args == (2,)
    # No notes task from the default room's unrelated history: one plain line.
    env.line.assert_awaited_once()
    assert env.line.await_args.args[1:] == (
        DEFAULT,
        f"meeting:{ROOM}:1790972400:no-transcript",
        "**Weekly sync** ended. No transcript was captured, so there are no notes.",
    )
    env.enqueue.assert_not_awaited()
    assert "notesRunId" not in env.meta["meeting"] and "notes" not in env.meta["meeting"]
    assert env.meta["meeting"]["notesSkipped"] == "no_transcript" and env.meta["meeting"]["notesMailedAt"]


async def test_on_ended_never_raises(env):
    env.enqueue.side_effect = RuntimeError("boom")
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)


async def test_app_notes_use_a_legacy_completion_marker_without_a_delivery_claim(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notesMailedAt"] and "notesMailAttemptedAt" not in env.meta["meeting"]


async def test_a_late_transcript_is_never_the_next_occurrences(env):
    env.meta["meeting"]["status"] = "scheduled"  # the series already advanced
    await mod.on_transcript(ROOM, [_line(0, "Pat", "Hi", 1.0)], True)
    env.store.assert_not_called()
    env.line.assert_not_awaited()
    env.metric.assert_called_once_with("meeting.transcript.dropped")


async def test_without_a_default_room_the_transcript_stays_in_the_meetings_room(env, monkeypatch):
    monkeypatch.setattr(
        mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: None if r == DEFAULT else env.meta)
    )
    await mod.on_transcript(ROOM, [_line(0, "Pat", "Hi", 1.0)], True)
    assert env.store.call_args.args[1] == ROOM and env.line.await_args.args[1] == ROOM


async def test_no_notes_or_mail_for_a_call_nobody_admitted_the_bot_to(env):
    del env.meta["meeting"]["inCallAt"]
    assert not mod.owed(env.meta["meeting"])
    await mod.on_ended(ROOM)
    env.claim.assert_not_awaited()
    env.line.assert_not_awaited()
    env.send.assert_not_awaited()
    env.advance.assert_awaited_once_with(ROOM)  # a series still moves on


async def test_the_output_rooms_front_agent_writes_the_notes(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.front.return_value = "front-agent"
    await mod.on_ended(ROOM)
    env.front.assert_awaited_with("org_1", room_id=DEFAULT)
    assert started(env)[1]["specialist"] == "front-agent" and env.requests[TRIG]["agentId"] == "front-agent"


async def test_on_ended_advances_after_the_attempt_either_way_but_not_beside_another(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.enqueue.side_effect = RuntimeError("ddb")  # fails before the task starts: the notes stay owed
    await mod.on_ended(ROOM)  # failed
    env.advance.assert_awaited_once_with(ROOM)
    env.claim.return_value = None  # another end path holds the notes: it advances when done
    await mod.on_ended(ROOM)
    env.advance.assert_awaited_once()


async def test_a_transcript_without_a_call_is_dropped(env):
    del env.meta["meeting"]["inCallAt"]
    await mod.on_transcript(ROOM, [_line(0, "Pat", "Hi", 1.0)], True)
    env.store.assert_not_called()
    env.metric.assert_called_once_with("meeting.transcript.dropped")


async def test_no_marker_lands_on_the_next_occurrence(env):
    """The series re-dated while the notes ran: yesterday's markers are not written onto today."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1

    async def redated(*_a, **_k):  # advance() won the race mid-notes
        env.meta["meeting"] = {**env.meta["meeting"], "startAt": 1791058800, "status": "scheduled"}
        env.meta["meeting"].pop("transcriptAttachedAt")
        return True

    env.enqueue.side_effect = redated
    await mod.on_ended(ROOM)
    assert "notesRunId" not in env.meta["meeting"] and "notesMailedAt" not in env.meta["meeting"]


@pytest.fixture
def legacy(env, monkeypatch):
    """A workspace with no default room and no organizer DM to open: the hidden meeting room is the
    output room, so the mail carries the transcript itself."""
    monkeypatch.setattr(
        mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: None if r == DEFAULT else env.meta)
    )
    monkeypatch.setattr(mod.meeting_invites, "_organizer_dm", AsyncMock(return_value=None))
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1")
    env.load = MagicMock(return_value=(None, b"[20:23] Pat: Hi all\n"))
    monkeypatch.setattr(mod.messaging, "load_attachment", env.load)
    return env


async def test_a_redate_during_the_wait_keeps_todays_key_and_tomorrows_run(env):
    today, tomorrow = env.meta["meeting"]["startAt"], env.meta["meeting"]["startAt"] + 86400

    async def redate(_s):  # a re-invite re-dated the series 30 s after the call ended
        env.meta["meeting"] = {**env.meta["meeting"], "startAt": tomorrow, "status": "scheduled"}
        env.meta["meeting"].pop("inCallAt")

    env.sleep.side_effect = redate
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 1  # stopped waiting on the other occurrence's read
    assert env.line.await_args.args[2] == f"meeting:{ROOM}:{today}:no-transcript"
    assert "notesRunId" not in env.meta["meeting"]
    # Tomorrow's call happens: its own notes run goes out under its own key.
    env.meta["meeting"].update(status="ended", inCallAt=1, transcriptAttachedAt=1)
    env.claim.return_value = "tok2"
    await mod.on_ended(ROOM)
    assert env.enqueue.await_args.args[1].trigger_id == f"meeting-notes:{ROOM}:{tomorrow}"
    assert env.enqueue.await_count == 1


async def test_three_failed_notes_attempts_still_advance_the_series(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.enqueue.side_effect = RuntimeError("ddb")
    await mod.on_ended(ROOM)
    assert env.enqueue.await_count == 3 and "notesMailedAt" not in env.meta["meeting"]
    env.send.assert_not_awaited()
    env.release.assert_awaited_once()
    env.advance.assert_awaited_once_with(ROOM)


async def test_a_retry_does_not_wait_for_the_transcript_again(env):
    env.line.side_effect = [RuntimeError("ddb"), {"messageId": "msg-1"}]
    await mod.on_ended(ROOM)  # no transcript: the 60 s wait once, then one 10 s retry gap
    assert [c.args for c in env.sleep.await_args_list] == [(2,)] * 30 + [(10,)]


UNSAID = [
    {"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "open PRs?", "answer": "Three.", "at": 5},
    {"runId": "r2", "agentId": "a2", "agentName": "Ledger", "request": "", "answer": "Burn is flat.", "at": 6},
]
_SAID = (
    "Answers the team had ready and did not get to say:\n\nForge: open PRs?\nThree.\n\n"
    "Ledger: (no request)\nBurn is flat."
)


async def test_a_repeated_task_end_keeps_the_notes_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    await task_done(env, "a retried Activity's other text")
    await task_done(env, to="failed")
    assert env.meta["meeting"]["notes"] == NOTES and env.requests[TRIG]["mailAttemptedAt"]
    env.send.assert_not_awaited()


async def test_no_agent_to_write_the_notes_is_counted_and_marked(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.front.side_effect = mod.task_conversation.NoFrontAgent("no seat")
    await mod.on_ended(ROOM)
    env.enqueue.assert_not_awaited()
    env.metric.assert_any_call("meeting.notes.skipped", dimensions={"why": "no_agent"})
    assert env.meta["meeting"]["notesSkipped"] == "no_agent" and "notesRunId" not in env.meta["meeting"]
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    env.send.assert_not_awaited()


async def test_a_failed_enqueue_is_retried_and_the_notes_are_kept_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.enqueue.side_effect = [RuntimeError("ddb"), True]
    await mod.on_ended(ROOM)
    assert env.enqueue.await_count == 2 and env.meta["meeting"]["notesRunId"] == TASK
    assert "notes" not in env.meta["meeting"]  # they wait for the task
    await task_done(env)
    assert env.meta["meeting"]["notes"] == NOTES and env.meta["meeting"]["notesMailedAt"]
    env.sleep.assert_awaited_once_with(10)
    env.advance.assert_awaited_once_with(ROOM)


async def test_notes_task_done_keeps_notes(env, remember):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_task_moved(notes_task(status="working"), "working")  # not an end
    await mod.on_task_moved({**notes_task(), "triggerId": "chat:other"}, "idle")  # not a notes task
    await mod.on_task_moved({**notes_task(), "taskId": "another-task"}, "idle")  # not this request's task
    assert "notes" not in env.meta["meeting"] and "notesMailedAt" not in env.meta["meeting"]
    await task_done(env, DECIDED)
    meeting = env.meta["meeting"]
    assert meeting["notes"] == DECIDED and meeting["notesMailedAt"]
    assert meeting["decisions"] == [
        {"text": "Closed on Dec 25 and Dec 26", "by": "Maya"},
        {"text": "Holiday hours are 10 to 4"},
    ]
    assert [f["text"] for f in meeting["followUps"]] == ["Pat: post the holiday hours"]
    assert env.requests[TRIG]["mailAttemptedAt"]


async def test_notes_task_failed_marks_meeting(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await task_done(env, to="failed")
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    assert not env.requests[TRIG].get("mailAttemptedAt")  # left for a Retry's follow-ups


async def test_a_person_stopped_notes_task_marks_meeting(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await task_done(env, to="resolved")  # Stop, Close or Dismiss
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    assert not env.requests[TRIG].get("mailAttemptedAt")


async def test_a_done_task_without_output_marks_no_notes(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await task_done(env, "  ")
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]


async def test_unsaid_answers_reach_the_notes_task(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, unsaid=UNSAID)
    await ended_and_noted(env)
    assert started(env)[1]["brief"].endswith(_SAID)


async def test_a_recurring_series_that_advanced_still_gets_its_notes_kept(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1")
    await mod.on_ended(ROOM)
    # advance(): the next occurrence replaces the live keys, the finished one moves into history
    env.meta["meeting"] = {
        "title": "Next week", "startAt": 1791577200, "status": "scheduled",
        "history": [{"startAt": 1790972400, "transcriptId": "att-1"}],
    }  # fmt: skip
    await task_done(env)
    assert env.meta["meeting"]["history"][0]["notes"] == NOTES
    assert "notes" not in env.meta["meeting"] and "notesMailedAt" not in env.meta["meeting"]
    assert env.requests[TRIG]["mailAttemptedAt"]
    env.send.assert_not_awaited()


async def test_the_notes_request_asks_for_action_items_not_paperclip_tasks(env):
    """8 Nov prod Meet: the request asked for "Paperclip tasks"; the writer refused ("Nova is not on the current
    Lightbulb team roster") and no action item became a thread."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    text = started(env)[1]["brief"]
    assert "Paperclip" not in text and "Action items:" in text and "do not create" in text


async def test_notes_record_commitments_once_without_authorizing_agent_work(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1)
    made = AsyncMock()
    monkeypatch.setattr(mod.messaging, "create_human_message", made)
    notes = "Action items:\n- Pat: send the holiday post\n- Ada: order nail gels"
    await ended_and_noted(env, notes)
    first = list(env.meta["meeting"]["followUps"])
    await mod._keep_notes("org_1", TRIG, env.requests[TRIG], notes + "\n- Nova: invent more work")
    await mod._file_action_items("org_1", "different-retry", env.requests[TRIG], notes)
    assert env.meta["meeting"]["followUps"] == first
    assert [f["text"] for f in first] == ["Pat: send the holiday post", "Ada: order nail gels"]
    assert len({f["id"] for f in first}) == 2
    assert all(f["status"] == "recorded" and not f.get("taskId") for f in first)
    made.assert_not_awaited()  # model-written notes are neither human messages nor authorization


async def test_notes_link_completed_work_without_rerunning_it(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1)
    made = AsyncMock()
    monkeypatch.setattr(mod.messaging, "create_human_message", made)
    env.meeting_tasks.return_value = [
        {"taskId": "t-done", "title": "Draft the holiday-hours post", "status": "idle", "triggerId": "chat:ask1"},
        {"taskId": "t-working", "title": "Order more gel colors from OPI", "status": "working"},
    ]
    notes = (
        "Action items:\n- Pulse: Draft the holiday-hours post\n- Vera: Order more gel colors from OPI\n"
        "- Pat: book the venue for the party"
    )
    await ended_and_noted(env, notes)
    await task_done(env, notes)  # a retried end
    done, working, human = env.meta["meeting"]["followUps"]
    assert (done["taskId"], done["status"], done["sourceTriggerId"]) == ("t-done", "done", "chat:ask1")
    assert (working["taskId"], working["status"]) == ("t-working", "working")
    assert human["status"] == "recorded" and not human.get("taskId")
    env.meeting_tasks.assert_awaited_with("org_1", ROOM, 1790972500, 1790976100)
    made.assert_not_awaited()


async def test_ambiguous_tasks_and_missing_call_window_stay_recorded(env):
    env.meeting_tasks.return_value = [
        {"taskId": "a", "title": "Draft holiday post", "status": "idle"},
        {"taskId": "b", "title": "Draft holiday post", "status": "working"},
    ]
    request = {"roomId": ROOM, "occurrence": env.meta["meeting"]["startAt"]}
    await mod._file_action_items("org_1", "msg-3", request, "Action items:\n- Pulse: draft holiday post")
    assert env.meta["meeting"]["followUps"][0]["status"] == "recorded"
    env.meta["meeting"].pop("inCallAt")
    env.meeting_tasks.reset_mock()
    await mod._file_action_items("org_1", "msg-4", request, "Action items:\n- Pat: book venue")
    env.meeting_tasks.assert_not_awaited()
    assert env.meta["meeting"]["followUps"][-1]["status"] == "recorded"


async def test_late_notes_record_actions_on_the_original_occurrence(env):
    occurrence = env.meta["meeting"]["startAt"]
    env.meta["meeting"].update(startAt=occurrence + 86400, history=[{"startAt": occurrence}])
    request = {"roomId": ROOM, "occurrence": occurrence, "meeting": {"inCallAt": 1790972500, "endedAt": 1790976100}}
    await mod._keep_notes("org_1", "late", request, "Action items:\n- Pat: book venue")
    assert not env.meta["meeting"].get("followUps")
    past = env.meta["meeting"]["history"][0]
    assert past["followUps"][0]["text"] == "Pat: book venue"
    assert past["followUps"][0]["status"] == "recorded"


@pytest.mark.parametrize(
    ("item", "title", "hit"),
    [
        ("Pulse: Draft the holiday-hours post", "Draft the holiday hours post", True),
        ("Pat: draft product test", "Draft product pivots test", False),
        ("Pulse: Finalize and share holiday-hours post", "Draft the holiday-hours post", False),
        ("Pat: book the venue", "Book the venue for the party", False),
    ],
)
def test_action_link_requires_exact_normalized_work(item, title, hit):
    assert mod.same_action(item, {"title": title}) is hit
    assert mod.same_action(item, {"description": title}) is hit


async def test_shared_keywords_never_mark_a_human_commitment_done(env):
    env.meeting_tasks.return_value = [{"taskId": "other", "title": "Draft product pivots test", "status": "idle"}]
    request = {"roomId": ROOM, "occurrence": env.meta["meeting"]["startAt"]}
    await mod._file_action_items("org_1", "msg-x", request, "Action items:\n- Pat: draft product test")
    assert env.meta["meeting"]["followUps"][0]["status"] == "recorded"
    assert not env.meta["meeting"]["followUps"][0].get("taskId")


async def test_the_meeting_tasks_are_the_rooms_threads_since_the_call(monkeypatch):
    """Only the meeting room's tasks are read (its THREAD rows), never a workspace-wide scan."""
    threads = [{"threadKey": f"room:{ROOM}:r1", "taskId": "t1", "history": ["t0", "later"]},
               {"threadKey": f"room:{ROOM}", "taskId": "t2", "history": []}]  # fmt: skip
    tasks = {
        "TASK#t0": {"taskId": "t0", "title": "Old", "createdAt": "2026-11-07T10:00:00+00:00"},
        "TASK#t1": {"taskId": "t1", "title": "Order gels", "createdAt": "2026-11-08T14:00:29+00:00"},
        "TASK#t2": {"taskId": "t2", "title": "Holiday post", "createdAt": "2026-11-08T13:57:42+00:00"},
    }
    tasks["TASK#later"] = {"taskId": "later", "title": "Next occurrence", "createdAt": "2026-11-09T13:57:42+00:00"}
    for task in tasks.values():
        task.update(origin="meeting", audience={"kind": "room", "roomId": ROOM, "userIds": []})
    rooms = AsyncMock(return_value=threads)
    monkeypatch.setattr(mod.tasks_repo, "list_room_threads", rooms)
    monkeypatch.setattr(
        mod.tasks_repo, "batch_get", AsyncMock(side_effect=lambda _w, sks: [tasks[k] for k in sks if k in tasks])
    )
    monkeypatch.setattr(mod.tasks_repo, "list_tasks_by_status", AsyncMock(side_effect=AssertionError("no scan")))
    since = 1794146120  # 2026-11-08T13:55:20Z, when Recall joined
    assert sorted(r["title"] for r in await mod._meeting_tasks("org_1", ROOM, since, since + 3600)) == [
        "Holiday post",
        "Order gels",
    ]
    rooms.assert_awaited_once_with("org_1", ROOM)


def test_the_assistants_lines_are_marked_and_never_action_items():
    text = mod._render([_line(0, "Pat", "Nova, draft the post", 1.0),
                        {**_line(1, "Nova", "Add the link once it's ready.", 2.0), "kind": "agent"}])  # fmt: skip
    assert "] Pat: Nova, draft the post" in text and "] Nova (the assistant): Add the link" in text


async def test_the_notes_request_says_the_assistants_lines_are_not_action_items(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    text = " ".join(started(env)[1]["brief"].split())
    assert "never from a line marked (the assistant)" in text


async def test_stored_notes_recover_admission_failure_before_consuming_completion_claim(env, monkeypatch):
    from core.voice.meetings import meeting_followups

    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    request = env.requests[TRIG]
    enqueue = AsyncMock(side_effect=[OSError("admission unavailable"), True])
    monkeypatch.setattr(meeting_followups, "enqueue", enqueue)
    with pytest.raises(OSError):
        await mod._keep_notes("org_1", TRIG, request, NOTES)
    assert env.meta["meeting"]["notes"] == NOTES and not request.get("mailAttemptedAt")
    await mod._keep_notes("org_1", TRIG, request, "a later conflicting reply")
    assert env.meta["meeting"]["notes"] == NOTES and request["mailAttemptedAt"] == 1
    assert enqueue.await_count == 2


DECIDED = (
    "We went over the holidays.\n\nDecisions:\n- Closed on Dec 25 and Dec 26 (decided by Maya)\n"
    "- Holiday hours are 10 to 4\n\nAction items:\n- Pat: post the holiday hours"
)


@pytest.fixture
def remember(monkeypatch):
    from core.services import memory_api

    r = AsyncMock(return_value={"queued": False, "neighbours": []})
    monkeypatch.setattr(memory_api, "remember", r)
    return r


async def test_the_notes_request_asks_for_a_decisions_section(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    text = started(env)[1]["brief"]
    assert '"Decisions:"' in text and "decided by <who>" in text


async def test_the_notes_runs_decisions_are_stored_on_the_meeting(env, remember):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env, DECIDED)
    assert env.meta["meeting"]["decisions"] == [
        {"text": "Closed on Dec 25 and Dec 26", "by": "Maya"},
        {"text": "Holiday hours are 10 to 4"},
    ]


async def test_decisions_go_into_team_memory_once_tagged_with_the_meeting_and_date(env, remember):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env, DECIDED)
    request = env.requests[TRIG]
    await mod._keep_notes("org_1", TRIG, request, DECIDED)  # a retry
    await task_done(env, DECIDED)
    assert remember.await_count == 2
    first = remember.await_args_list[0].kwargs
    assert first["kind"] == "decision" and first["scope"] == "team" and first["subject"] == "Decision: Weekly sync"
    assert (
        first["fact"]
        == "Decided in the meeting Weekly sync on 2026-10-02: Closed on Dec 25 and Dec 26 (decided by Maya)"
    )
    assert env.meta["meeting"]["decisionsRemembered"] == [0, 1]
    assert env.requests[TRIG]["mailAttemptedAt"]


async def test_a_failed_memory_write_is_retried_without_rewriting_the_others(env, remember):
    """Codex P2 on #5186 and #5288: a transient remember error is a failed keep, retried in place."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    remember.side_effect = [{"queued": False}, RuntimeError("member rpc"), {"queued": False}]
    await ended_and_noted(env, DECIDED)
    facts = [c.kwargs["fact"] for c in remember.await_args_list]
    assert len(facts) == 3 and facts[1] == facts[2] and facts[0] != facts[2]  # the failed one, once more
    assert env.meta["meeting"]["decisionsRemembered"] == [0, 1]
    assert env.requests[TRIG]["mailAttemptedAt"]
    env.sleep.assert_awaited_once_with(0.5)


async def test_a_secret_shaped_decision_is_not_retried(env, remember):
    from core.services import memory_api

    env.meta["meeting"]["transcriptAttachedAt"] = 1
    remember.side_effect = [memory_api.Refused("secret"), {"queued": False}]
    await ended_and_noted(env, DECIDED)
    assert env.meta["meeting"]["decisionsRemembered"] == [0, 1] and env.requests[TRIG]["mailAttemptedAt"]
    assert remember.await_count == 2


async def test_a_meeting_with_no_decisions_writes_nothing_to_memory(env, remember):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env, "Nothing was settled.\n\nAction items:\n- Pat: ask again")
    assert env.meta["meeting"]["decisions"] == []
    remember.assert_not_awaited()
    assert "decisionsRemembered" not in env.meta["meeting"]


async def test_late_decisions_land_on_the_original_occurrence(env, remember):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1")
    await mod.on_ended(ROOM)
    env.meta["meeting"] = {
        "title": "Next week", "startAt": 1791577200, "status": "scheduled",
        "history": [{"startAt": 1790972400, "transcriptId": "att-1"}],
    }  # fmt: skip
    await task_done(env, DECIDED)
    past = env.meta["meeting"]["history"][0]
    assert len(past["decisions"]) == 2 and past["decisionsRemembered"] == [0, 1]
    assert "decisions" not in env.meta["meeting"] and "decisionsRemembered" not in env.meta["meeting"]
    assert remember.await_args_list[0].kwargs["subject"] == "Decision: Weekly sync"


async def test_the_worker_reads_the_transcript_by_reference(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1", transcriptRoomId=DEFAULT)
    load = MagicMock(return_value=(None, b"[20:23] Pat: Hi all\n"))
    monkeypatch.setattr(mod.messaging, "load_attachment", load)
    await mod.on_ended(ROOM)
    block = await mod.worker_transcript("org_1", TRIG)
    load.assert_called_once_with("org_1", DEFAULT, "att-1")
    assert block == "## Transcript\n[20:23] Pat: Hi all\n"
    assert await mod.worker_transcript("org_1", "chat:not-notes") is None


async def test_long_transcript_is_cut_from_start_and_flagged(env, monkeypatch, caplog):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1", transcriptRoomId=DEFAULT)
    lines = [f"[10:{i % 60:02d}] Pat: line {i} of the meeting\n" for i in range(20000)]
    raw = "".join(lines).encode()
    assert len(raw) > mod.TRANSCRIPT_MAX_BYTES
    monkeypatch.setattr(mod.messaging, "load_attachment", MagicMock(return_value=(None, raw)))
    meta = SimpleNamespace(size=len(raw))
    monkeypatch.setattr(mod.messaging, "load_attachment_head", MagicMock(return_value=(meta, b"[")))
    await mod.on_ended(ROOM)
    with caplog.at_level("WARNING", logger=mod.logger.name):
        block = await mod.worker_transcript("org_1", TRIG)
    head, body = block.split("\n", 2)[1], block.split("\n", 2)[2]
    assert "cut" in head and "last part" in head  # the Worker is told
    assert body.endswith(lines[-1]) and body.startswith("[") and lines[0] not in body  # the end whole, by line
    assert len(body.encode()) <= mod.TRANSCRIPT_MAX_BYTES
    warning = next(r for r in caplog.records if "cut" in r.getMessage())
    assert ROOM in warning.getMessage() and str(len(raw)) in warning.getMessage()
    assert "line 19999" not in warning.getMessage()  # sizes, never transcript text
    await task_done(env)
    assert env.meta["meeting"]["notes"] == f"{mod.CUT_LINE}\n\n{NOTES}"  # the stored notes say so


async def test_retry_after_failure_keeps_notes(env):
    """A failed notes task spends the claim with no notes; a person's Retry that succeeds still stores them."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await task_done(env, to="failed")
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    await task_done(env, DECIDED)  # the Retry's attempt is done
    assert env.meta["meeting"]["notes"] == DECIDED and len(env.meta["meeting"]["decisions"]) == 2
    await task_done(env, "a later attempt's other text")
    assert env.meta["meeting"]["notes"] == DECIDED  # written once


async def test_a_notes_trigger_the_router_never_started_marks_no_notes(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_not_started("org_1", {"triggerId": "chat:other"})  # not a notes trigger
    assert "notesMailedAt" not in env.meta["meeting"]
    await mod.on_not_started("org_1", {"triggerId": TRIG})
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]


async def test_retry_after_failure_admits_followups(env, monkeypatch):
    """Codex P2 on #5288: a failed completion spent the marker meeting_followups.enqueue reads, so a successful
    Retry kept the notes but admitted no follow-up work."""
    from core.voice.meetings import meeting_followups

    seen = []
    enqueue = AsyncMock(side_effect=lambda _o, _r, request: seen.append(dict(request)) or True)
    monkeypatch.setattr(meeting_followups, "enqueue", enqueue)
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await task_done(env, to="failed")
    enqueue.assert_not_awaited()
    await task_done(env)  # the Retry's attempt is done
    enqueue.assert_awaited_once()
    assert not seen[0].get("mailAttemptedAt")  # admitted as a fresh completion
    assert env.requests[TRIG]["mailAttemptedAt"]  # spent once the notes landed


def _record_followups(monkeypatch):
    from core.voice.meetings import meeting_followups

    seen = []
    enqueue = AsyncMock(side_effect=lambda _o, rid, request: seen.append((rid, dict(request))) or True)
    monkeypatch.setattr(meeting_followups, "enqueue", enqueue)
    return enqueue, seen


async def test_keep_retries_then_succeeds(env, monkeypatch):
    enqueue, _ = _record_followups(monkeypatch)
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    enqueue.side_effect = [OSError("admission unavailable"), True]
    await task_done(env)
    assert env.meta["meeting"]["notes"] == NOTES and env.requests[TRIG]["mailAttemptedAt"]
    assert enqueue.await_count == 2
    env.sleep.assert_awaited_once_with(0.5)


async def test_keep_is_safe_to_repeat(env, monkeypatch, remember):
    """Every step of a keep repeats harmlessly: one notes record, one follow-up trigger id, no duplicate action
    items, each decision remembered once."""
    enqueue, seen = _record_followups(monkeypatch)
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    request = dict(env.requests[TRIG])  # the request as read before either keep
    assert await mod._keep_notes("org_1", TRIG, request, DECIDED)
    assert await mod._keep_notes("org_1", TRIG, request, DECIDED + "\n- Nova: one more")
    meeting = env.meta["meeting"]
    assert meeting["notes"] == DECIDED
    assert [f["text"] for f in meeting["followUps"]] == ["Pat: post the holiday hours"]
    assert {rid for rid, _ in seen} == {TRIG}  # one request: meeting_followups' trigger id is derived from it
    assert remember.await_count == 2 and meeting["decisionsRemembered"] == [0, 1]


async def test_keep_failure_after_retries_marks_no_notes(env, caplog):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    env.patch_failures = 3  # every try fails to write the meeting
    with caplog.at_level("ERROR", logger=mod.logger.name):
        await task_done(env)
    meeting = env.meta["meeting"]
    assert meeting["notesMailedAt"] and "notes" not in meeting  # "no notes" at once, never left pending
    assert [c.args for c in env.sleep.await_args_list] == [(0.5,), (1.0,)]
    error = next(r for r in caplog.records if r.levelname == "ERROR")
    assert ROOM in error.getMessage() and TASK in error.getMessage() and "Decisions" not in error.getMessage()
    env.metric.assert_any_call("meeting.notes.keep_failed")
    assert not env.requests[TRIG].get("mailAttemptedAt")  # a Retry that succeeds still keeps them
    await mod.on_task_moved(notes_task(status="resolved"), "resolved")  # the quiet resolve: no second pass
    assert "notes" not in env.meta["meeting"]


async def test_an_expired_notes_request_ends_quietly(env, monkeypatch):
    monkeypatch.setattr(mod.messaging_repo, "get_meeting_notes_request", AsyncMock(return_value=None))
    with pytest.raises(mod.TranscriptUnavailable):
        await mod.worker_transcript("org_1", TRIG)
    await task_done(env, to="failed")  # the meeting record's request is gone: nothing to mark, no error
    assert "notesMailedAt" not in env.meta["meeting"]
    assert not [c for c in env.metric.call_args_list if c.args and c.args[0] == "meeting.notes.mail_failed"]


async def test_keep_total_time_is_bounded(env, monkeypatch, caplog):
    """Review on #5288: a keep that outlasts end_attempt's 30 s runs beside the activity's retry."""
    import asyncio as real_asyncio

    async def hangs(*_a, **_k):
        await real_asyncio.Event().wait()  # a remember call that never answers

    monkeypatch.setattr(mod, "_keep_notes", hangs)
    monkeypatch.setattr(mod, "KEEP_TOTAL_S", 0.05)
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    with caplog.at_level("ERROR", logger=mod.logger.name):
        await task_done(env)  # returns: never raises into the activity
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    error = next(r for r in caplog.records if r.levelname == "ERROR")
    assert ROOM in error.getMessage() and TASK in error.getMessage()


async def test_an_unreadable_transcript_says_try_again(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1", transcriptRoomId=DEFAULT)
    await mod.on_ended(ROOM)
    monkeypatch.setattr(mod.messaging, "load_attachment", MagicMock(side_effect=OSError("s3 timeout")))
    with pytest.raises(mod.TranscriptUnavailable, match="couldn't be read just now"):
        await mod.worker_transcript("org_1", TRIG)
    gone = mod.messaging.ApiProblem("MESSAGE_NOT_FOUND", "Attachment not found.")
    monkeypatch.setattr(mod.messaging, "load_attachment", MagicMock(side_effect=gone))
    with pytest.raises(mod.TranscriptUnavailable, match="no longer available"):
        await mod.worker_transcript("org_1", TRIG)
