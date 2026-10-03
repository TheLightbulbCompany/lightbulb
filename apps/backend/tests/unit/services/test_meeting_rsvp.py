"""meeting_rsvp: agents@ answers a member's invite with one METHOD:REPLY, once per sequence."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.services import meeting_invites, meeting_rsvp

ADDRESS = "agents@acme.thelightbulb.company"
INVITE = meeting_invites.Invite(
    uid="abc@google.com",
    sequence=2,
    method="REQUEST",
    start=datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc),
    end=datetime(2026, 10, 3, 19, 30, tzinfo=timezone.utc),
    summary="Sync, Q4; plan\\notes",
    description="",
    organizer_email="prasiddha@acme.com",
    attendee_emails=[],
    meeting_url="https://meet.google.com/abc-defg-hij",
)


@pytest.fixture
def env(monkeypatch):
    state = {"meta": {"meeting": {"uid": INVITE.uid, "sequence": 2}}}
    send = AsyncMock(return_value="<m@x>")
    repo = meeting_invites.messaging_repo
    monkeypatch.setattr(repo, "get_room", AsyncMock(side_effect=lambda _r: state["meta"]))
    monkeypatch.setattr(repo, "patch_room_meta", AsyncMock(side_effect=lambda _r, u: state["meta"].update(u)))
    monkeypatch.setattr(meeting_rsvp.agent_email, "team_address", AsyncMock(return_value=ADDRESS))
    monkeypatch.setattr(meeting_rsvp.agent_mail_tenant, "ensure_tenant", AsyncMock(return_value="tenant-1"))
    monkeypatch.setattr(meeting_rsvp.ses_email, "send_ses_email", send)
    return state, send


def _unfold(text):
    return text.replace("\r\n ", "")


async def test_accept_sends_one_reply_and_records_the_sequence(env):
    state, send = env
    await meeting_rsvp.accept("org_1", INVITE, "room-1")
    send.assert_awaited_once()
    kw = send.await_args.kwargs
    assert kw["from_addr"] == ADDRESS and kw["to"] == "prasiddha@acme.com"
    assert kw["subject"] == "Accepted: Sync, Q4; plan\\notes" and kw["kind"] == "meeting_rsvp"
    assert kw["tenant_name"] == "tenant-1" and kw["headers"] == {"Auto-Submitted": "auto-generated"}
    cal = _unfold(kw["calendar"])
    assert "METHOD:REPLY\r\n" in cal and "UID:abc@google.com\r\n" in cal and "SEQUENCE:2\r\n" in cal
    assert f"ATTENDEE;PARTSTAT=ACCEPTED;CN=Lightbulb agents:mailto:{ADDRESS}\r\n" in cal
    assert "DTSTART:20261003T190000Z\r\nDTEND:20261003T193000Z\r\n" in cal
    assert "SUMMARY:Sync\\, Q4\; plan\\\\notes\r\n" in cal
    assert state["meta"]["meeting"]["rsvpSequence"] == 2


async def test_same_sequence_sends_nothing_and_a_bump_sends_again(env):
    state, send = env
    await meeting_rsvp.accept("org_1", INVITE, "room-1")
    await meeting_rsvp.accept("org_1", INVITE, "room-1")
    send.assert_awaited_once()
    await meeting_rsvp.accept("org_1", replace(INVITE, sequence=3), "room-1")
    assert send.await_count == 2 and state["meta"]["meeting"]["rsvpSequence"] == 3


async def test_a_failed_send_is_not_recorded(env):
    state, send = env
    send.return_value = None
    await meeting_rsvp.accept("org_1", INVITE, "room-1")
    assert "rsvpSequence" not in state["meta"]["meeting"]


async def test_no_team_address_sends_nothing(env, monkeypatch):
    _, send = env
    monkeypatch.setattr(meeting_rsvp.agent_email, "team_address", AsyncMock(return_value=None))
    await meeting_rsvp.accept("org_1", INVITE, "room-1")
    send.assert_not_awaited()


async def test_accept_swallows_a_raising_send(env):
    state, send = env
    send.side_effect = RuntimeError("boom")
    await meeting_rsvp.accept("org_1", INVITE, "room-1")  # does not raise
    assert "rsvpSequence" not in state["meta"]["meeting"]


def test_a_long_uid_folds_and_unfolds():
    uid = "u" * 200 + "@google.com"
    ics = meeting_rsvp.reply_ics(
        replace(INVITE, uid=uid, end=None), ADDRESS, datetime(2026, 10, 1, tzinfo=timezone.utc)
    )
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert f"\r\nUID:{uid}\r\n" in _unfold(ics)
    assert "DTEND" not in ics and "DTSTAMP:20261001T000000Z" in ics
