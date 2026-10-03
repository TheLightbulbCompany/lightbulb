"""meeting_rsvp: agents@ answers a member's invite with one METHOD:REPLY."""

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
    attendee_emails=["prasiddha@acme.com", "Agents@Acme.thelightbulb.company"],
    meeting_url="https://meet.google.com/abc-defg-hij",
)
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def send(monkeypatch):
    send = AsyncMock(return_value="<m@x>")
    monkeypatch.setattr(meeting_rsvp.agent_email, "team_address", AsyncMock(return_value=ADDRESS))
    monkeypatch.setattr(meeting_rsvp.agent_mail_tenant, "ensure_tenant", AsyncMock(return_value="tenant-1"))
    monkeypatch.setattr(meeting_rsvp.ses_email, "send_ses_email", send)
    return send


def _unfold(text):
    return text.replace("\r\n ", "")


async def test_accept_sends_one_reply(send):
    assert await meeting_rsvp.accept("org_1", INVITE) is True
    send.assert_awaited_once()
    kw = send.await_args.kwargs
    assert kw["from_addr"] == ADDRESS and kw["to"] == "prasiddha@acme.com"
    assert kw["subject"] == "Accepted: Sync, Q4; plan\\notes" and kw["kind"] == "meeting_rsvp"
    assert kw["tenant_name"] == "tenant-1" and kw["headers"] == {"Auto-Submitted": "auto-generated"}
    cal = _unfold(kw["calendar"])
    assert "METHOD:REPLY\r\n" in cal and "UID:abc@google.com\r\n" in cal and "SEQUENCE:2\r\n" in cal
    assert f"ATTENDEE;PARTSTAT=ACCEPTED;CN=Lightbulb agents:mailto:{ADDRESS}\r\n" in cal
    assert "DTSTART" not in cal and "DTEND" not in cal
    assert r"SUMMARY:Sync\, Q4\; plan\\notes" + "\r\n" in cal


async def test_a_multiline_summary_stays_one_subject_line(send):
    await meeting_rsvp.accept("org_1", replace(INVITE, summary="Sync\nwith  team"))
    assert send.await_args.kwargs["subject"] == "Accepted: Sync with team"


async def test_a_forwarded_invite_is_not_answered(send):
    assert await meeting_rsvp.accept("org_1", replace(INVITE, attendee_emails=["prasiddha@acme.com"])) is False
    send.assert_not_awaited()


async def test_no_team_address_sends_nothing(send, monkeypatch):
    monkeypatch.setattr(meeting_rsvp.agent_email, "team_address", AsyncMock(return_value=None))
    assert await meeting_rsvp.accept("org_1", INVITE) is False
    send.assert_not_awaited()


async def test_a_failed_send_is_false(send):
    send.return_value = None
    assert await meeting_rsvp.accept("org_1", INVITE) is False


async def test_accept_swallows_a_raising_send(send):
    send.side_effect = RuntimeError("boom")
    assert await meeting_rsvp.accept("org_1", INVITE) is False


@pytest.mark.parametrize("octets", [150, 224])
def test_a_long_uid_folds_at_75_octets(octets):
    uid = "u" * (octets - len("UID:"))
    ics = meeting_rsvp.reply_ics(replace(INVITE, uid=uid), ADDRESS, NOW)
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert f"\r\nUID:{uid}\r\n" in _unfold(ics)
    assert "DTSTAMP:20261001T000000Z" in ics


def test_a_non_ascii_summary_folds_without_splitting_a_character():
    ics = meeting_rsvp.reply_ics(replace(INVITE, summary="é" * 80), ADDRESS, NOW)
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert f"\r\nSUMMARY:{'é' * 80}\r\n" in _unfold(ics)
