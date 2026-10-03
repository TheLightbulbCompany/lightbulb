"""Invites to the team address: the parser, and the member/stranger split in handle_team_mail."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import meeting_invites

GOOGLE = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "BEGIN:STANDARD",
        "DTSTART:19701101T020000",
        "TZOFFSETFROM:-0400",
        "TZOFFSETTO:-0500",
        "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "DTSTART;TZID=America/New_York:20261003T150000",
        "DTEND;TZID=America/New_York:20261003T153000",
        "DTSTAMP:20261002T120000Z",
        "ORGANIZER;CN=Prasiddha Parthsarthy:mailto:Prasiddha@acme.com",
        "UID:7kukuqrfedlm2f9t0vr42q2gjv@google.com",
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=",
        " TRUE;CN=agents@acme.thelightbulb.company;X-NUM-GUESTS=0:mailto:agents@acme.",
        " thelightbulb.company",
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN=Prasid",
        " dha Parthsarthy;X-NUM-GUESTS=0:mailto:prasiddha@acme.com",
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij",
        "DESCRIPTION:Weekly sync\\, bring numbers.\\n\\nJoin with Google Meet: https://m",
        " eet.google.com/abc-defg-hij\\nOr dial: (US) +1 555-0100 PIN: 123456#",
        "LOCATION:",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "SUMMARY:Acme weekly sync",
        "TRANSP:OPAQUE",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:This is an event reminder",
        "TRIGGER:-P0DT0H10M0S",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)

OUTLOOK = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "PRODID:Microsoft Exchange Server 2010",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        'ORGANIZER;CN="Lee, Dana":mailto:dana@acme.com',
        'ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN="agents@ac',
        ' me.thelightbulb.company":mailto:agents@acme.thelightbulb.company',
        "DESCRIPTION;LANGUAGE=en-US:Agenda to follow.\\n",
        "UID:040000008200E00074C5B7101A82E00800000000A0B1C2D3E4F5A6B7C8D9E0F10000000",
        " 01000000001234567890ABCDEF",
        "SUMMARY;LANGUAGE=en-US:Pipeline review",
        "DTSTART;TZID=Pacific Standard Time:20261005T090000",
        "DTEND;TZID=Pacific Standard Time:20261005T100000",
        "LOCATION;LANGUAGE=en-US:https://meet.google.com/xyz-abcd-efg",
        "SEQUENCE:2",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


def test_parse_google_invite():
    inv = meeting_invites.parse(GOOGLE)
    assert inv.uid == "7kukuqrfedlm2f9t0vr42q2gjv@google.com"
    assert inv.method == "REQUEST" and inv.sequence == 0
    assert inv.start == datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)
    assert inv.end == datetime(2026, 10, 3, 19, 30, tzinfo=timezone.utc)
    assert inv.summary == "Acme weekly sync"
    assert inv.description.startswith("Weekly sync, bring numbers.\n\nJoin with Google Meet: https://meet.google.com/")
    assert inv.organizer_email == "prasiddha@acme.com"
    assert inv.attendee_emails == ["agents@acme.thelightbulb.company", "prasiddha@acme.com"]
    assert inv.meeting_url == "https://meet.google.com/abc-defg-hij"


def test_parse_outlook_invite():
    inv = meeting_invites.parse(OUTLOOK)
    assert inv.uid.endswith("01000000001234567890ABCDEF")
    assert inv.sequence == 2
    assert inv.start == datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)  # PDT
    assert inv.organizer_email == "dana@acme.com"
    assert inv.attendee_emails == ["agents@acme.thelightbulb.company"]
    assert inv.meeting_url == "https://meet.google.com/xyz-abcd-efg"


def test_cancel_is_recognized():
    inv = meeting_invites.parse(GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1"))
    assert inv.method == "CANCEL" and inv.sequence == 1


def test_no_vevent_or_uid_is_none():
    assert meeting_invites.parse("BEGIN:VCALENDAR\r\nEND:VCALENDAR") is None
    assert meeting_invites.parse(GOOGLE.replace("UID:", "X-UID:")) is None


def test_utc_and_date_only_starts():
    utc = meeting_invites.parse(
        OUTLOOK.replace("DTSTART;TZID=Pacific Standard Time:20261005T090000", "DTSTART:20261005T090000Z")
    )
    assert utc.start == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    day = meeting_invites.parse(
        OUTLOOK.replace("DTSTART;TZID=Pacific Standard Time:20261005T090000", "DTSTART;VALUE=DATE:20261005")
    )
    assert day.start == datetime(2026, 10, 5, tzinfo=timezone.utc)


TEAM = {"owner_id": "org_1", "agent_id": "ceo-1", "address": "agents@acme.thelightbulb.company", "verified_senders": []}


@pytest.fixture
def repo(monkeypatch):
    mocks = SimpleNamespace(
        put=AsyncMock(),
        get=AsyncMock(return_value=None),
        set_status=AsyncMock(),
        members=AsyncMock(return_value=[{"email": "Prasiddha@acme.com"}]),
    )
    monkeypatch.setattr(meeting_invites.meetings_repo, "put_meeting", mocks.put)
    monkeypatch.setattr(meeting_invites.meetings_repo, "get_meeting", mocks.get)
    monkeypatch.setattr(meeting_invites.meetings_repo, "set_status", mocks.set_status)
    monkeypatch.setattr(meeting_invites.clerk_admin, "list_organization_members", mocks.members)
    return mocks


async def test_a_members_invite_becomes_a_meeting_row(repo):
    out = await meeting_invites.handle_team_mail("org_1", SimpleNamespace(calendar=GOOGLE), TEAM)
    assert out == "meeting_scheduled"
    row = repo.put.await_args.args[0]
    assert row["uid"] == "7kukuqrfedlm2f9t0vr42q2gjv@google.com"
    assert row["status"] == "scheduled" and row["createdBy"] == "prasiddha@acme.com"
    assert row["startAt"] == int(datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc).timestamp())


async def test_a_strangers_invite_schedules_nothing(repo):
    repo.members.return_value = [{"email": "someone-else@acme.com"}]
    out = await meeting_invites.handle_team_mail("org_1", SimpleNamespace(calendar=GOOGLE), TEAM)
    assert out is None  # the route delivers it to the CEO as mail
    repo.put.assert_not_awaited()


async def test_plain_team_mail_is_mail(repo):
    assert await meeting_invites.handle_team_mail("org_1", SimpleNamespace(calendar=None), TEAM) is None
    repo.members.assert_not_awaited()


async def test_cancel_marks_the_row_cancelled(repo):
    repo.get.return_value = {"sequence": 0, "organizer": "prasiddha@acme.com", "status": "scheduled"}
    cal = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", SimpleNamespace(calendar=cal), TEAM) == "meeting_cancelled"
    repo.set_status.assert_awaited_once_with("org_1", "7kukuqrfedlm2f9t0vr42q2gjv@google.com", "cancelled", sequence=1)


async def test_an_older_sequence_does_not_overwrite(repo):
    repo.get.return_value = {"sequence": 3, "organizer": "prasiddha@acme.com", "status": "scheduled"}
    assert await meeting_invites.handle_team_mail("org_1", SimpleNamespace(calendar=GOOGLE), TEAM) == "meeting_stale"
    repo.put.assert_not_awaited()
