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
        meta["meeting"] = {**meta["meeting"], **fields}
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
    m.task_titles = AsyncMock(return_value=[])
    monkeypatch.setattr(mod, "_meeting_task_titles", m.task_titles)
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
    assert args[3].endswith("The transcript is attached")
    assert kwargs["dispatch_targets"] == [{"agentId": CEO, "eventKind": "user_request"}]
    assert env.meta["meeting"]["notesRunId"] == mod.messaging_repo.run_id_for(CEO, "msg-1")
    env.send.assert_not_awaited()  # the mail waits for the notes themselves
    assert env.requests["msg-1"] == {
        "roomId": ROOM, "occurrence": 1790972400, "targetRoomId": DEFAULT, "agentId": CEO, "triggerSeq": 7,
        "meeting": {"title": "Weekly sync", "organizer": "pat@acme.com", "transcriptAttachedAt": 1,
                    "inCallAt": 1790972500},
    }  # fmt: skip
    env.sleep.assert_not_awaited()
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    env.send.assert_awaited_once()
    assert env.send.await_args.kwargs["body_text"].startswith(NOTES)


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
    env.send.assert_awaited_once()


async def test_the_organizer_mail_carries_the_notes_and_the_room_url_and_no_meeting_url(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    kw = env.send.await_args.kwargs
    assert kw["body_text"].startswith(NOTES)
    assert kw["from_addr"] == "agents@acme.thelightbulb.company" and kw["to"] == "pat@acme.com"
    assert kw["subject"] == "Notes: Weekly sync"
    assert f"https://flickmeon.ai/chat?room={DEFAULT}" in kw["body_text"]
    assert MEET not in kw["body_text"] and MEET not in kw["subject"]


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


async def test_a_notes_mail_that_fails_before_sending_is_counted_and_never_doubled(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.cap.side_effect = RuntimeError("ddb")  # fails before the send
    await ended_and_noted(env)
    env.send.assert_not_awaited()
    env.metric.assert_any_call("meeting.notes.mail_failed")
    await mod.on_ended(ROOM)  # a later terminal webhook: the notes run already has the mail
    env.line.assert_awaited_once()
    env.send.assert_not_awaited()


async def test_a_failed_or_ambiguous_send_is_never_retried_by_a_later_webhook(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.send.return_value = None  # send_ses_email's non-raising failure: it may still have gone
    await ended_and_noted(env)
    assert "notesMailedAt" not in env.meta["meeting"] and env.requests["msg-1"]["mailAttemptedAt"]
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)  # a replayed reply
    assert not mod.owed(env.meta["meeting"])
    env.send.return_value = "<id@ses>"
    await mod.on_ended(ROOM)  # the terminal webhook's redelivery: nothing more
    await mod.on_ended(ROOM)
    env.send.assert_awaited_once()
    env.line.assert_awaited_once()


async def test_two_end_paths_racing_send_once(env):
    env.members.return_value = [{"kind": "human", "id": "user_1"}]  # no notes run: the meeting's marker guards
    stale = {**env.meta["meeting"]}  # the second path read the meeting before the first marked it
    await mod.on_ended(ROOM)
    env.send.assert_awaited_once()
    assert await mod._mail_organizer("org_1", ROOM, DEFAULT, stale, "Weekly sync", stale["startAt"]) is None
    env.send.assert_awaited_once()  # the guarded marker write refused it


async def test_the_reply_and_the_run_end_send_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "error"})
    env.send.assert_awaited_once()


async def test_a_pre_send_skip_is_done_without_an_attempted_marker(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["outbound_disabled"] = "complaint"
    await ended_and_noted(env)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notesMailedAt"] and "notesMailAttemptedAt" not in env.meta["meeting"]


async def test_the_notes_mail_draws_the_owners_daily_cap(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await ended_and_noted(env)
    env.cap.assert_awaited_once_with("org_1")
    env.send.assert_awaited_once()


async def test_a_spent_daily_cap_sends_nothing_and_is_not_retried(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.cap.return_value = False
    await ended_and_noted(env)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notesMailedAt"]  # a deliberate non-send: done
    env.release.assert_not_awaited()
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    env.cap.assert_awaited_once()


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
    env.send.assert_awaited_once()


async def test_a_founder_roster_without_starter_ids_still_gets_its_notes(env):
    """Prod 2026-10-04: a company row with no ``starter_agent_ids`` skipped the notes silently."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.ceo.return_value = None
    env.members.return_value = [{"kind": "agent", "id": "forge"}]
    await ended_and_noted(env)
    assert env.line.await_args.kwargs["dispatch_targets"] == [{"agentId": "forge", "eventKind": "user_request"}]
    assert env.meta["meeting"]["notesRunId"] == mod.messaging_repo.run_id_for("forge", "msg-1")
    assert env.send.await_args.kwargs["subject"] == "Notes: Weekly sync"


async def test_no_agent_to_write_the_notes_is_counted_marked_and_said_in_the_mail(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.members.return_value = [{"kind": "human", "id": "user_1"}]
    await mod.on_ended(ROOM)
    env.line.assert_not_awaited()
    env.metric.assert_any_call("meeting.notes.skipped", dimensions={"why": "no_agent"})
    assert env.meta["meeting"]["notesSkipped"] == "no_agent" and "notesRunId" not in env.meta["meeting"]
    kw = env.send.await_args.kwargs
    assert kw["subject"] == "Transcript: Weekly sync"
    assert kw["body_text"].endswith("No notes were written: no agent in the room could write them.")


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


async def test_with_nowhere_visible_the_mail_carries_the_notes_and_the_transcript_and_no_link(legacy):
    await ended_and_noted(legacy)
    kw = legacy.send.await_args.kwargs
    assert kw["subject"] == "Notes: Weekly sync"
    assert kw["body_text"] == f"{NOTES}\n\nTranscript:\n\n[20:23] Pat: Hi all\n"
    assert "/chat?room=" not in kw["body_text"]
    legacy.load.assert_called_once_with("org_1", ROOM, "att-1")


async def test_the_mail_that_carries_the_transcript_also_says_when_no_notes_were_written(legacy):
    legacy.members.return_value = [{"kind": "human", "id": "user_1"}]
    await mod.on_ended(ROOM)
    kw = legacy.send.await_args.kwargs
    assert kw["subject"] == "Transcript: Weekly sync"
    assert kw["body_text"].endswith("\n\nNo notes were written: no agent in the room could write them.")


async def test_a_long_transcript_is_cut_and_says_so(legacy):
    legacy.load.return_value = (None, b"x" * 300_000)
    await ended_and_noted(legacy)
    body = legacy.send.await_args.kwargs["body_text"]
    assert body.endswith("x" * 10 + "… (truncated)") and body.count("x") == 200_000


async def test_without_a_transcript_the_mail_says_none_was_captured(legacy):
    legacy.meta["meeting"].pop("transcriptId")
    await ended_and_noted(legacy)
    assert legacy.send.await_args.kwargs["body_text"].endswith("No transcript was captured.")
    assert legacy.send.await_args.kwargs["subject"] == "Notes: Weekly sync"


async def test_the_mail_reads_the_transcript_from_the_room_it_was_stored_for(legacy):
    legacy.meta["meeting"]["transcriptRoomId"] = "stored-room"
    await ended_and_noted(legacy)
    legacy.load.assert_called_once_with("org_1", "stored-room", "att-1")


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


async def test_the_after_meeting_mail_goes_to_the_inviting_member(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, invitedBy="member@acme.com", organizer="host@outside.example")
    await ended_and_noted(env)
    assert env.send.await_args.kwargs["to"] == "member@acme.com"


async def test_a_failed_notes_line_is_retried_and_the_mail_goes_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = [RuntimeError("ddb"), {"messageId": "msg-1"}]
    await mod.on_ended(ROOM)
    assert env.line.await_count == 2 and env.meta["meeting"]["notesRunId"]
    env.send.assert_not_awaited()  # the mail waits for the notes
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    env.send.assert_awaited_once()
    assert env.meta["meeting"]["notesMailedAt"]
    env.sleep.assert_awaited_once_with(10)
    env.release.assert_not_awaited()
    env.advance.assert_awaited_once_with(ROOM)


async def test_three_failed_notes_attempts_still_advance_the_series(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = RuntimeError("ddb")
    await mod.on_ended(ROOM)
    assert env.line.await_count == 3 and "notesMailedAt" not in env.meta["meeting"]
    env.send.assert_not_awaited()
    env.release.assert_awaited_once()
    env.advance.assert_awaited_once_with(ROOM)


async def test_with_no_transcript_the_mail_promises_no_notes(env):
    await mod.on_ended(ROOM)
    kw = env.send.await_args.kwargs
    assert kw["subject"] == "Ended: Weekly sync"
    assert kw["body_text"] == "Weekly sync ended. No transcript was captured, so there are no notes."


async def test_a_retry_does_not_wait_for_the_transcript_again(env):
    env.line.side_effect = [RuntimeError("ddb"), {"messageId": "msg-1"}]
    await mod.on_ended(ROOM)  # no transcript: the 60 s wait once, then one 10 s retry gap
    assert [c.args for c in env.sleep.await_args_list] == [(2,)] * 30 + [(10,)]
    assert env.send.await_count == 1 and env.meta["meeting"]["notesMailedAt"]


async def test_an_ambiguous_send_is_never_repeated_in_one_end_path(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.send.return_value = None  # failed or timed out: it may have gone
    await ended_and_noted(env)
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)  # a replayed reply
    env.send.assert_awaited_once()
    assert "notesMailedAt" not in env.meta["meeting"]
    env.advance.assert_awaited_once_with(ROOM)


async def test_the_organizer_mail_is_from_the_public_address(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["public_address"] = "agents@thelightbulb.company"
    await ended_and_noted(env)
    assert env.send.await_args.kwargs["from_addr"] == "agents@thelightbulb.company"


UNSAID = [
    {"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "open PRs?", "answer": "Three.", "at": 5},
    {"runId": "r2", "agentId": "a2", "agentName": "Ledger", "request": "", "answer": "Burn is flat.", "at": 6},
]
_SAID = (
    "Answers the team had ready and did not get to say:\n\nForge: open PRs?\nThree.\n\n"
    "Ledger: (no request)\nBurn is flat."
)


async def test_unsaid_answers_reach_the_notes_run_which_works_them_into_the_mailed_notes(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, unsaid=UNSAID)
    await ended_and_noted(env)
    assert env.line.await_args.args[3].endswith(_SAID)
    assert _SAID not in env.send.await_args.kwargs["body_text"]  # the notes carry them, once


async def test_unsaid_answers_reach_the_mail_when_no_notes_were_written(legacy):
    legacy.meta["meeting"]["unsaid"] = UNSAID
    legacy.members.return_value = [{"kind": "human", "id": "user_1"}]  # no agent: no notes run
    await mod.on_ended(ROOM)
    assert legacy.send.await_args.kwargs["body_text"].endswith(_SAID)


async def test_only_the_notes_agents_own_final_reply_is_mailed(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_agent_reply("org_1", CEO, "msg-1", "Reading the transcript now.", True)  # progress
    await mod.on_agent_reply("org_1", CEO, "other-msg", "Unrelated answer.", False)  # another request
    await mod.on_agent_reply("org_1", CEO, None, "Top-level post.", False)
    await mod.on_agent_reply("org_1", "another-agent", "msg-1", "Not the notes.", False)  # not the writer
    env.send.assert_not_awaited()
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    env.send.assert_awaited_once()


async def test_a_notes_run_that_fails_still_mails_once_and_says_so(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "other-msg", "outcome": "error"})  # not a notes run
    env.send.assert_not_awaited()
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "error"})
    kw = env.send.await_args.kwargs
    assert kw["subject"] == "Transcript: Weekly sync"
    assert kw["body_text"].startswith("Weekly sync ended. The notes could not be written this time.")
    assert f"https://flickmeon.ai/chat?room={DEFAULT}" in kw["body_text"]
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "aborted"})
    env.send.assert_awaited_once()


async def test_a_reply_that_beat_its_request_record_is_mailed_when_the_run_ends(env):
    """The agent answered before _notes recorded the request: on_agent_reply found nothing, so the
    run's end reads the agent's final reply from the room."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    env.posted[:] = [
        {"author": {"id": CEO}, "replyToMessageId": "msg-1", "text": "Reading it now.", "progress": True},
        {"author": {"id": "someone"}, "replyToMessageId": "msg-1", "text": "Not the notes."},
        {"author": {"id": CEO}, "replyToMessageId": "msg-1", "text": NOTES},
    ]
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    kw = env.send.await_args.kwargs
    assert kw["subject"] == "Notes: Weekly sync" and kw["body_text"].startswith(NOTES)


async def test_a_recurring_series_that_advanced_still_gets_its_notes_mailed(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, invitedBy="ada@acme.com")
    await mod.on_ended(ROOM)
    env.meta["meeting"] = {"title": "Next week", "startAt": 1791577200, "status": "scheduled"}  # advance()
    await mod.on_agent_reply("org_1", CEO, "msg-1", NOTES, False)
    kw = env.send.await_args.kwargs
    assert kw["to"] == "ada@acme.com" and kw["subject"] == "Notes: Weekly sync"
    assert "notesMailedAt" not in env.meta["meeting"]  # never written onto the next occurrence


async def test_a_failed_notes_run_of_an_advanced_series_still_mails_the_unsaid_answers(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, unsaid=UNSAID)
    await mod.on_ended(ROOM)
    env.meta["meeting"] = {"title": "Next week", "startAt": 1791577200, "status": "scheduled"}  # advance()
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "error"})
    assert env.send.await_args.kwargs["body_text"].endswith(_SAID)


async def test_the_notes_request_asks_for_action_items_not_paperclip_tasks(env):
    """8 Nov prod Meet: the request asked for "Paperclip tasks"; the writer refused ("Nova is not on the current
    Lightbulb team roster") and no action item became a thread."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    text = env.line.await_args.args[3]
    assert "Paperclip" not in text and "Action items:" in text and "do not create" in text


async def test_each_action_item_becomes_a_thread_through_the_meeting_rooms_router(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1, invitedBy="pat@acme.com")
    env.members.return_value = [{"kind": "human", "id": "user_1", "role": "owner"}, {"kind": "agent", "id": CEO}]
    made = AsyncMock(return_value=SimpleNamespace(message_id="m-ai"))
    monkeypatch.setattr(mod.messaging, "create_human_message", made)
    notes = ("Decisions: ship Friday.\n\nAction items:\n- Pat: send the holiday post\n* Ada: order nail gels\n\n"
             "Thanks all.")  # fmt: skip
    await ended_and_noted(env, notes)
    await mod.on_notes_run_ended("org_1", {"triggerMessageId": "msg-1", "outcome": "completed"})
    auth, room, req = made.await_args.args
    assert (auth.user_id, auth.org_id, room) == ("user_1", "org_1", ROOM)  # the meeting's room, the inviter
    assert req.text == ("Action items from the meeting Weekly sync, one thread each:\n- Pat: send the holiday post\n"
                        "- Ada: order nail gels")  # fmt: skip
    assert made.await_args.kwargs == {"via": "meeting_notes"}  # nobody spoke it: no spoken-reply hint
    ids = {c.args[2].client_message_id for c in made.await_args_list}
    assert len(ids) == 1  # the reply and the run's end: one message


async def test_notes_with_no_action_items_file_nothing(env, monkeypatch):
    env.meta["meeting"].update(transcriptAttachedAt=1, invitedBy="pat@acme.com")
    made = AsyncMock()
    monkeypatch.setattr(mod.messaging, "create_human_message", made)
    await ended_and_noted(env, "Decisions: none. Action items: none.")
    made.assert_not_awaited()


async def test_action_items_the_meeting_already_started_are_not_filed_again(env, monkeypatch):
    """8 Nov prod Meet re-run: LB-24 "Finalize and share holiday-hours post" duplicated LB-19, which had finished."""
    env.meta["meeting"].update(transcriptAttachedAt=1, invitedBy="pat@acme.com")
    env.members.return_value = [{"kind": "human", "id": "user_1", "role": "owner"}, {"kind": "agent", "id": CEO}]
    made = AsyncMock(return_value=SimpleNamespace(message_id="m-ai"))
    monkeypatch.setattr(mod.messaging, "create_human_message", made)
    env.task_titles.return_value = ["Draft the holiday-hours post", "Order more gel colors from OPI"]
    notes = ("Action items:\n- Pulse: Finalize and share holiday-hours post\n- Vera: order the gel colours\n"
             "- Pat: book the venue for the party")  # fmt: skip
    await ended_and_noted(env, notes)
    assert made.await_args.args[2].text.endswith("one thread each:\n- Pat: book the venue for the party")
    assert env.task_titles.await_args.args == ("org_1", ROOM, 1790972500)  # the meeting room's, since the call
    env.task_titles.return_value = ["Draft the holiday-hours post", "Order more gel colors", "Book the venue"]
    made.reset_mock()
    env.meta["meeting"].pop("notesMailedAt", None)
    await mod._file_action_items("org_1", "msg-2", {"roomId": ROOM, "meeting": {}}, notes)
    made.assert_not_awaited()  # all covered: nothing to file


def test_covered_matches_content_words_not_any_overlap():
    assert mod.covered("Pulse: Finalize and share holiday-hours post", ["Draft the holiday-hours post"])
    assert not mod.covered("Pat: send the holiday post", ["Update the holiday hours on Google"])  # one word: post
    assert not mod.covered("Ada: order nail gels", ["Book a nail appointment"])
    assert not mod.covered("Ada: order nail gels", [])


async def test_the_meeting_tasks_are_the_rooms_since_the_call(monkeypatch):
    rows = {
        "working": [{"taskId": "t1", "title": "Order gels", "createdAt": "2026-11-08T14:00:29+00:00",
                     "audience": {"kind": "workspace"}}],
        "idle": [{"taskId": "t2", "title": "Holiday post", "createdAt": "2026-11-08T13:57:42+00:00",
                  "audience": {"kind": "room", "roomId": ROOM}},
                 {"taskId": "t3", "title": "Old", "createdAt": "2026-11-07T10:00:00+00:00",
                  "audience": {"kind": "room", "roomId": ROOM}}],
    }  # fmt: skip
    monkeypatch.setattr(
        mod.tasks_repo, "list_tasks_by_status", AsyncMock(side_effect=lambda _w, s, **_: (rows.get(s, []), None))
    )
    trees = {"t1": {"task": {}, "subscriptions": [{"createdBy": "router", "threadKey": f"room:{ROOM}"}]}}
    monkeypatch.setattr(
        mod.tasks_repo, "get_task_tree", AsyncMock(side_effect=lambda _w, t: trees.get(t, {"task": {}}))
    )
    since = 1794146120  # 2026-11-08T13:55:20Z, when Recall joined
    assert sorted(await mod._meeting_task_titles("org_1", ROOM, since)) == ["Holiday post", "Order gels"]


def test_the_assistants_lines_are_marked_and_never_action_items():
    text = mod._render([_line(0, "Pat", "Nova, draft the post", 1.0),
                        {**_line(1, "Nova", "Add the link once it's ready.", 2.0), "kind": "agent"}])  # fmt: skip
    assert "] Pat: Nova, draft the post" in text and "] Nova (the assistant): Add the link" in text


async def test_the_notes_request_says_the_assistants_lines_are_not_action_items(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    text = " ".join(env.line.await_args.args[3].split())
    assert "never from a line marked (the assistant)" in text
