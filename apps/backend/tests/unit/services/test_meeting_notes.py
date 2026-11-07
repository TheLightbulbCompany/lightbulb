"""A meeting's transcript lands in its room once; its end runs one notes turn and mails the organizer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import meeting_notes as mod

ROOM = "11111111-1111-4111-8111-111111111111"
CEO = "22222222-2222-4222-8222-222222222222"
MEET = "https://meet.google.com/abc-defg-hij"
DEFAULT = mod.messaging.default_room_id("org_1")  # the output room: every line lands there


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
        },
    }

    async def patch_meeting(room_id, fields, drop=(), when=None):
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
        row={"address": "agents@acme.thelightbulb.company"},
        sleep=AsyncMock(),
        claim=AsyncMock(return_value="tok"),
        release=AsyncMock(),
        cap=AsyncMock(return_value=True),
        members=AsyncMock(return_value=[{"kind": "human", "id": "user_1"}, {"kind": "agent", "id": CEO}]),
        ceo=AsyncMock(return_value=CEO),
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
    monkeypatch.setattr(mod.openclaw_work, "_fallback_ceo_id", m.ceo)
    monkeypatch.setattr(mod.meeting_invites, "advance", m.advance)
    monkeypatch.setattr(mod, "put_metric", m.metric)
    monkeypatch.setattr(mod.container_repo, "release_owner_lifecycle_claim", m.release)
    monkeypatch.setattr(mod.agent_mail_send, "consume_cap", m.cap)
    monkeypatch.setattr(mod.container_repo, "try_claim_owner_lifecycle", m.claim)
    monkeypatch.setattr(mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta))
    monkeypatch.setattr(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting))
    monkeypatch.setattr(mod.meeting_invites, "_post_line", m.line)
    monkeypatch.setattr(mod.messaging, "store_attachment", m.store)
    monkeypatch.setattr(mod.agent_email, "team_address", AsyncMock(return_value="agents@acme.thelightbulb.company"))
    monkeypatch.setattr(mod.agent_email_repo, "get_by_address", AsyncMock(side_effect=lambda _a: m.row))
    monkeypatch.setattr(mod.agent_mail_tenant, "ensure_tenant", AsyncMock(return_value="tenant-1"))
    monkeypatch.setattr(mod.ses_email, "send_ses_email", m.send)
    monkeypatch.setattr(mod.settings, "FRONTEND_URL", "https://flickmeon.ai")
    monkeypatch.setattr(mod.asyncio, "sleep", m.sleep)
    return m


NOTES = "Decisions: ship the beta Friday. Open: pricing. Tasks filed: ISOA-1 (Pat), ISOA-2 (Ada)."


async def ended_and_noted(env, notes=NOTES):
    """The call ends, then the notes agent's reply to the notes request (msg-1) arrives."""
    await mod.on_ended(ROOM)
    writer = (env.requests.get("msg-1") or {}).get("agentId", CEO)
    await mod.on_agent_reply("org_1", writer, "msg-1", notes, False)


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


async def test_on_ended_dispatches_exactly_one_notes_run(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_ended(ROOM)
    env.line.assert_awaited_once()
    args, kwargs = env.line.await_args.args, env.line.await_args.kwargs
    assert args[1:3] == (DEFAULT, f"meeting:{ROOM}:1790972400:notes")
    env.members.assert_awaited_with(DEFAULT)
    assert args[3].startswith("Write the meeting notes for Weekly sync: decisions, open questions")
    assert args[3].endswith("the transcript is attached")
    assert kwargs["dispatch_targets"] == [{"agentId": CEO, "eventKind": "user_request"}]
    assert env.meta["meeting"]["notesRunId"] == mod.messaging_repo.run_id_for(CEO, "msg-1")
    assert env.requests["msg-1"] == {
        "roomId": ROOM, "occurrence": 1790972400, "targetRoomId": DEFAULT, "agentId": CEO, "triggerSeq": 7,
        "meeting": {"title": "Weekly sync", "organizer": "pat@acme.com", "transcriptAttachedAt": 1},
    }  # fmt: skip
    env.sleep.assert_not_awaited()
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)


async def test_meeting_notes_not_emailed(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notes"] == NOTES.strip()  # the app's copy, for the timeline


async def test_on_ended_waits_for_the_transcript_then_proceeds(env):
    async def attach_on_second(_s):
        if env.sleep.await_count == 2:
            env.meta["meeting"]["transcriptAttachedAt"] = 1

    env.sleep.side_effect = attach_on_second
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 2
    env.line.assert_awaited_once()


async def test_on_ended_proceeds_without_a_transcript_after_sixty_seconds(env):
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 30 and env.sleep.await_args.args == (2,)
    # No notes run from the default room's unrelated history: one plain line, then the mail.
    env.line.assert_awaited_once()
    assert env.line.await_args.args[1:] == (
        DEFAULT,
        f"meeting:{ROOM}:1790972400:no-transcript",
        "**Weekly sync** ended. No transcript was captured, so there are no notes.",
    )
    assert "dispatch_targets" not in env.line.await_args.kwargs and "notesRunId" not in env.meta["meeting"]
    assert env.meta["meeting"]["notesSkipped"] == "no_transcript"


async def test_a_paused_team_address_sends_no_mail(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["outbound_disabled"] = "complaint"
    await ended_and_noted(env)
    env.line.assert_awaited_once()
    env.send.assert_not_awaited()


async def test_on_ended_never_raises(env):
    env.line.side_effect = RuntimeError("boom")
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)


async def test_a_pre_send_skip_is_done_without_an_attempted_marker(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["outbound_disabled"] = "complaint"
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


async def test_a_ceo_outside_the_output_room_hands_the_notes_to_an_agent_that_is_in_it(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.members.return_value = [{"kind": "agent", "id": "b-agent"}, {"kind": "agent", "id": "a-agent"}]
    await ended_and_noted(env)
    assert env.line.await_args.kwargs["dispatch_targets"] == [{"agentId": "a-agent", "eventKind": "user_request"}]


async def test_a_founder_roster_without_starter_ids_still_gets_its_notes(env):
    """Prod 2026-10-04: a company row with no ``starter_agent_ids`` skipped the notes silently."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.ceo.return_value = None
    env.members.return_value = [{"kind": "agent", "id": "forge"}]
    await ended_and_noted(env)
    assert env.line.await_args.kwargs["dispatch_targets"] == [{"agentId": "forge", "eventKind": "user_request"}]
    assert env.meta["meeting"]["notesRunId"] == mod.messaging_repo.run_id_for("forge", "msg-1")


async def test_on_ended_advances_after_the_attempt_either_way_but_not_beside_another(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = RuntimeError("ddb")  # fails before the mail: the notes stay owed
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
        return {"messageId": "msg-1"}

    env.line.side_effect = redated
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
    assert env.line.await_args.args[2] == f"meeting:{ROOM}:{tomorrow}:notes" and env.line.await_count == 2


async def test_three_failed_notes_attempts_still_advance_the_series(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = RuntimeError("ddb")
    await mod.on_ended(ROOM)
    assert env.line.await_count == 3 and "notesMailedAt" not in env.meta["meeting"]
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


async def test_the_reply_and_the_run_end_keep_the_notes_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "error"})
    assert env.meta["meeting"]["notes"] == NOTES and env.requests["msg-1"]["mailAttemptedAt"]
    env.send.assert_not_awaited()


async def test_no_agent_to_write_the_notes_is_counted_and_marked(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.members.return_value = [{"kind": "human", "id": "user_1"}]
    await mod.on_ended(ROOM)
    env.line.assert_not_awaited()
    env.metric.assert_any_call("meeting.notes.skipped", dimensions={"why": "no_agent"})
    assert env.meta["meeting"]["notesSkipped"] == "no_agent" and "notesRunId" not in env.meta["meeting"]
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]
    env.send.assert_not_awaited()


async def test_a_failed_notes_line_is_retried_and_the_notes_are_kept_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = [RuntimeError("ddb"), {"messageId": "msg-1"}]
    await mod.on_ended(ROOM)
    assert env.line.await_count == 2 and env.meta["meeting"]["notesRunId"]
    assert "notes" not in env.meta["meeting"]  # they wait for the agent's reply
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    assert env.meta["meeting"]["notes"] == NOTES and env.meta["meeting"]["notesMailedAt"]
    env.sleep.assert_awaited_once_with(10)
    env.advance.assert_awaited_once_with(ROOM)


async def test_only_the_notes_agents_own_final_reply_is_kept(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_agent_reply("org_1", CEO, "msg-1", "Reading the transcript now.", True)  # progress
    await mod.on_agent_reply("org_1", CEO, "other-msg", "Unrelated answer.", False)  # another request
    await mod.on_agent_reply("org_1", CEO, None, "Top-level post.", False)
    await mod.on_agent_reply("org_1", "another-agent", "msg-1", "Not the notes.", False)  # not the writer
    assert "notes" not in env.meta["meeting"]
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    assert env.meta["meeting"]["notes"] == NOTES


async def test_a_notes_run_that_fails_keeps_no_notes_and_is_done(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "other-msg", "outcome": "error"})  # not a notes run
    assert "notesMailedAt" not in env.meta["meeting"]
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "error"})
    assert env.meta["meeting"]["notesMailedAt"] and "notes" not in env.meta["meeting"]


async def test_a_reply_that_beat_its_request_record_is_kept_when_the_run_ends(env):
    """The agent answered before _notes recorded the request: the run's end reads its final reply from the room."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    env.posted[:] = [
        {"author": {"id": CEO}, "replyToMessageId": "msg-1", "text": "Reading it now.", "progress": True},
        {"author": {"id": "someone"}, "replyToMessageId": "msg-1", "text": "Not the notes."},
        {"author": {"id": CEO}, "replyToMessageId": "msg-1", "text": NOTES},
    ]
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    assert env.meta["meeting"]["notes"] == NOTES


async def test_unsaid_answers_reach_the_notes_run(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, unsaid=UNSAID)
    await ended_and_noted(env)
    assert env.line.await_args.args[3].endswith(_SAID)


async def test_a_recurring_series_that_advanced_still_gets_its_notes_kept(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1")
    await mod.on_ended(ROOM)
    # advance(): the next occurrence replaces the live keys, the finished one moves into history
    env.meta["meeting"] = {
        "title": "Next week", "startAt": 1791577200, "status": "scheduled",
        "history": [{"startAt": 1790972400, "transcriptId": "att-1"}],
    }  # fmt: skip
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    assert env.meta["meeting"]["history"][0]["notes"] == NOTES
    assert "notes" not in env.meta["meeting"] and "notesMailedAt" not in env.meta["meeting"]
    assert env.requests["msg-1"]["mailAttemptedAt"]
    env.send.assert_not_awaited()
