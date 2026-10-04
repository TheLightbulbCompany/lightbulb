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
        line=AsyncMock(return_value={"messageId": "msg-1"}),
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
    env.send.assert_awaited_once()
    env.sleep.assert_not_awaited()


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


async def test_the_organizer_mail_carries_the_room_url_and_no_meeting_url(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    kw = env.send.await_args.kwargs
    assert kw["from_addr"] == "agents@acme.thelightbulb.company" and kw["to"] == "pat@acme.com"
    assert kw["subject"] == "Notes: Weekly sync"
    assert f"https://flickmeon.ai/chat?room={DEFAULT}" in kw["body_text"]
    assert MEET not in kw["body_text"] and MEET not in kw["subject"]


async def test_a_paused_team_address_sends_no_mail(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["outbound_disabled"] = "complaint"
    await mod.on_ended(ROOM)
    env.line.assert_awaited_once()
    env.send.assert_not_awaited()


async def test_on_ended_never_raises(env):
    env.line.side_effect = RuntimeError("boom")
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)


async def test_a_crash_after_the_claim_is_finished_by_a_later_on_ended(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.cap.side_effect = [RuntimeError("ddb")] * 3 + [True]  # fails before the send: still owed
    await mod.on_ended(ROOM)  # the notes message went out, the mail did not
    assert env.meta["meeting"]["notesRunId"] and "notesMailedAt" not in env.meta["meeting"]
    env.release.assert_awaited_once_with(f"meeting-notes#{ROOM}", token="tok")  # a caught failure frees the claim
    env.claim.return_value = None  # a crashed attempt released nothing: within 120 s its claim still stands
    await mod.on_ended(ROOM)
    env.send.assert_not_awaited()
    env.claim.return_value = "tok2"  # 121 s later it is stale
    await mod.on_ended(ROOM)
    assert env.claim.await_args.args == (f"meeting-notes#{ROOM}",) and env.claim.await_args.kwargs == {"stale_s": 120}
    env.line.assert_awaited_once()  # the notes run is not asked for twice
    assert env.send.await_count == 1 and env.meta["meeting"]["notesMailedAt"]
    await mod.on_ended(ROOM)  # done: nothing more
    assert env.send.await_count == 1


async def test_a_failed_or_ambiguous_send_is_never_retried_by_a_later_webhook(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.send.return_value = None  # send_ses_email's non-raising failure: it may still have gone
    await mod.on_ended(ROOM)
    assert "notesMailedAt" not in env.meta["meeting"] and env.meta["meeting"]["notesMailAttemptedAt"]
    assert not mod.owed(env.meta["meeting"])
    env.send.return_value = "<id@ses>"
    await mod.on_ended(ROOM)  # the terminal webhook's redelivery: nothing more
    await mod.on_ended(ROOM)
    env.send.assert_awaited_once()
    env.line.assert_awaited_once()


async def test_two_end_paths_racing_send_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    stale = {**env.meta["meeting"]}  # the second path read the meeting before the first marked it
    await mod.on_ended(ROOM)
    env.send.assert_awaited_once()
    assert await mod._mail_organizer("org_1", ROOM, DEFAULT, stale, "Weekly sync", stale["startAt"]) is None
    env.send.assert_awaited_once()  # the guarded marker write refused it


async def test_a_pre_send_skip_is_done_without_an_attempted_marker(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["outbound_disabled"] = "complaint"
    await mod.on_ended(ROOM)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notesMailedAt"] and "notesMailAttemptedAt" not in env.meta["meeting"]


async def test_the_notes_mail_draws_the_owners_daily_cap(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    env.cap.assert_awaited_once_with("org_1")
    env.send.assert_awaited_once()


async def test_a_spent_daily_cap_sends_nothing_and_is_not_retried(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.cap.return_value = False
    await mod.on_ended(ROOM)
    env.send.assert_not_awaited()
    assert env.meta["meeting"]["notesMailedAt"]  # a deliberate non-send: done
    env.release.assert_not_awaited()
    await mod.on_ended(ROOM)
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
    await mod.on_ended(ROOM)
    assert env.line.await_args.kwargs["dispatch_targets"] == [{"agentId": "a-agent", "eventKind": "user_request"}]
    env.send.assert_awaited_once()


async def test_a_founder_roster_without_starter_ids_still_gets_its_notes(env):
    """Prod 2026-10-04: a company row with no ``starter_agent_ids`` skipped the notes silently."""
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.ceo.return_value = None
    env.members.return_value = [{"kind": "agent", "id": "forge"}]
    await mod.on_ended(ROOM)
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
    """A workspace with no default room: the hidden meeting room is the output room."""
    monkeypatch.setattr(
        mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: None if r == DEFAULT else env.meta)
    )
    env.meta["meeting"].update(transcriptAttachedAt=1, transcriptId="att-1")
    env.load = MagicMock(return_value=(None, b"[20:23] Pat: Hi all\n"))
    monkeypatch.setattr(mod.messaging, "load_attachment", env.load)
    return env


async def test_without_a_default_room_the_mail_carries_the_transcript_and_no_link(legacy):
    await mod.on_ended(ROOM)
    kw = legacy.send.await_args.kwargs
    assert kw["subject"] == "Transcript: Weekly sync"
    assert kw["body_text"] == "Your Lightbulb agents joined Weekly sync.\n\n[20:23] Pat: Hi all\n"
    assert "/chat?room=" not in kw["body_text"]
    legacy.load.assert_called_once_with("org_1", ROOM, "att-1")


async def test_a_long_transcript_is_cut_and_says_so(legacy):
    legacy.load.return_value = (None, b"x" * 300_000)
    await mod.on_ended(ROOM)
    body = legacy.send.await_args.kwargs["body_text"]
    assert body.endswith("x" * 10 + "… (truncated)") and body.count("x") == 200_000


async def test_without_a_transcript_the_mail_says_none_was_captured(legacy):
    legacy.meta["meeting"].pop("transcriptId")
    await mod.on_ended(ROOM)
    assert legacy.send.await_args.kwargs["body_text"].endswith("No transcript was captured.")
    assert legacy.send.await_args.kwargs["subject"] == "Ended: Weekly sync"


async def test_the_mail_reads_the_transcript_from_the_room_it_was_stored_for(legacy):
    legacy.meta["meeting"]["transcriptRoomId"] = "stored-room"
    await mod.on_ended(ROOM)
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
    await mod.on_ended(ROOM)
    assert env.send.await_args.kwargs["to"] == "member@acme.com"


async def test_a_failed_notes_line_is_retried_and_the_mail_goes_once(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.line.side_effect = [RuntimeError("ddb"), {"messageId": "msg-1"}]
    await mod.on_ended(ROOM)
    assert env.line.await_count == 2 and env.meta["meeting"]["notesRunId"]
    env.send.assert_awaited_once()  # on the attempt that reached it
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
    await mod.on_ended(ROOM)
    env.send.assert_awaited_once()  # across the three attempts
    assert "notesMailedAt" not in env.meta["meeting"]
    env.advance.assert_awaited_once_with(ROOM)


async def test_the_organizer_mail_is_from_the_public_address(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    env.row["public_address"] = "agents@thelightbulb.company"
    await mod.on_ended(ROOM)
    assert env.send.await_args.kwargs["from_addr"] == "agents@thelightbulb.company"


UNSAID = [
    {"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "open PRs?", "answer": "Three.", "at": 5},
    {"runId": "r2", "agentId": "a2", "agentName": "Ledger", "request": "", "answer": "Burn is flat.", "at": 6},
]
_SAID = (
    "Answers the team had ready and did not get to say:\n\nForge: open PRs?\nThree.\n\n"
    "Ledger: (no request)\nBurn is flat."
)


async def test_unsaid_answers_reach_the_notes_run_and_the_mail(env):
    env.meta["meeting"].update(transcriptAttachedAt=1, unsaid=UNSAID)
    await mod.on_ended(ROOM)
    assert env.line.await_args.args[3].endswith(_SAID)
    assert env.send.await_args.kwargs["body_text"].endswith(_SAID)


async def test_unsaid_answers_reach_the_mail_that_carries_the_transcript(legacy):
    legacy.meta["meeting"]["unsaid"] = UNSAID
    await mod.on_ended(ROOM)
    assert legacy.send.await_args.kwargs["body_text"].endswith(_SAID)
