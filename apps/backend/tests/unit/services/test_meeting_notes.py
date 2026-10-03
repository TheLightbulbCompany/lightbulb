"""A meeting's transcript lands in its room once; its end runs one notes turn and mails the organizer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import meeting_notes as mod

ROOM = "11111111-1111-4111-8111-111111111111"
CEO = "22222222-2222-4222-8222-222222222222"
MEET = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def env(monkeypatch):
    meta = {
        "roomId": ROOM,
        "ownerId": "org_1",
        "meeting": {"title": "Weekly sync", "organizer": "pat@acme.com", "meetingUrl": MEET},
    }

    async def patch_meeting(room_id, fields, drop=()):
        meta["meeting"] = {**meta["meeting"], **fields}
        return meta["meeting"]

    async def set_if_absent(room_id, field, value, also=None):
        if field in meta["meeting"]:
            return False
        meta["meeting"] = {**meta["meeting"], field: value}
        return True

    attachment = SimpleNamespace(model_dump=lambda **_: {"id": "att-1", "fileName": "transcript.txt"})
    m = SimpleNamespace(
        meta=meta,
        store=MagicMock(return_value=attachment),
        line=AsyncMock(return_value={"messageId": "msg-1"}),
        send=AsyncMock(return_value="<id@ses>"),
        row={"address": "agents@acme.thelightbulb.company"},
        sleep=AsyncMock(),
    )
    monkeypatch.setattr(mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta))
    monkeypatch.setattr(mod.messaging_repo, "set_meeting_field_if_absent", AsyncMock(side_effect=set_if_absent))
    monkeypatch.setattr(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting))
    monkeypatch.setattr(mod.meeting_invites, "_post_line", m.line)
    monkeypatch.setattr(mod.messaging, "store_attachment", m.store)
    company = SimpleNamespace(starter_agent_ids={"ceo": CEO})
    monkeypatch.setattr(
        mod, "PaperclipRepo", lambda **_: SimpleNamespace(resolve_owner_row=AsyncMock(return_value=company))
    )
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
    assert (owner, room, name, ctype) == ("org_1", ROOM, "transcript.txt", "text/plain")
    assert data.decode() == "[20:23] Pat: Hi all\n[20:24] Ada: Hello\n"  # UTC, by mark
    env.line.assert_awaited_once()
    assert env.line.await_args.args[3] == "Transcript attached."
    assert env.line.await_args.kwargs["attachments"] == [{"id": "att-1", "fileName": "transcript.txt"}]
    assert env.meta["meeting"]["transcriptAttachedAt"]
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


async def test_on_ended_proceeds_without_a_transcript_after_twenty_seconds(env):
    await mod.on_ended(ROOM)
    assert env.sleep.await_count == 10
    env.line.assert_awaited_once()
    text = env.line.await_args.args[3]
    assert text.endswith("no transcript was captured; write notes from the room history")
    assert "the transcript is attached" not in text


async def test_the_organizer_mail_carries_the_room_url_and_no_meeting_url(env):
    env.meta["meeting"]["transcriptAttachedAt"] = 1
    await mod.on_ended(ROOM)
    kw = env.send.await_args.kwargs
    assert kw["from_addr"] == "agents@acme.thelightbulb.company" and kw["to"] == "pat@acme.com"
    assert kw["subject"] == "Notes: Weekly sync"
    assert f"https://flickmeon.ai/chat?room={ROOM}" in kw["body_text"]
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
