"""Invites to the team address: the parser, and the member/stranger split in handle_team_mail."""

import time
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import meeting_invites
from tests.unit.repositories.test_messaging_repo import FRIDAY, OWNER, USER_A, _member, table  # noqa: F401

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
        "DTSTART;TZID=America/Los_Angeles:20261005T090000",
        "DTEND;TZID=America/Los_Angeles:20261005T100000",
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


def test_a_windows_tzid_is_none():
    assert meeting_invites.parse(OUTLOOK.replace("America/Los_Angeles", "Pacific Standard Time")) is None


@pytest.mark.parametrize(
    "bad",
    [
        ("SEQUENCE:0", "SEQUENCE:abc"),
        ("DTSTART;TZID=America/New_York:", "DTSTART;TZID=America:"),
        ("UID:7kukuqrfedlm2f9t0vr42q2gjv@google.com", "UID:" + "x" * 513),
        ("SEQUENCE:0", "RRULE:FREQ=WEEKLY;BYDAY=XX\r\nSEQUENCE:0"),
        ("SEQUENCE:0", "RRULE:FREQ=MINUTELY\r\nSEQUENCE:0"),
    ],
    ids=["bad-sequence", "zone-directory", "uid-too-long", "bad-rrule", "minutely"],
)
def test_unusable_invites_are_none(bad):
    assert meeting_invites.parse(GOOGLE.replace(*bad)) is None


def test_a_non_meet_conference_url_is_not_trusted():
    zoom = GOOGLE.replace(
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij", "X-GOOGLE-CONFERENCE:https://evil.example/j/1"
    )
    assert meeting_invites.parse(zoom).meeting_url == "https://meet.google.com/abc-defg-hij"  # from DESCRIPTION
    only = zoom.replace("Join with Google Meet: https://m", "Join: https://x").replace(
        " eet.google.com/abc-defg-hij", " .example/"
    )
    assert meeting_invites.parse(only).meeting_url is None


def test_attendees_are_capped():
    many = "\r\n".join(f"ATTENDEE:mailto:p{i}@acme.com" for i in range(150))
    assert len(meeting_invites.parse(GOOGLE.replace("SEQUENCE:0", many + "\r\nSEQUENCE:0")).attendee_emails) == 100


def test_utc_and_date_only_starts():
    utc = meeting_invites.parse(
        OUTLOOK.replace("DTSTART;TZID=America/Los_Angeles:20261005T090000", "DTSTART:20261005T090000Z")
    )
    assert utc.start == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    day = meeting_invites.parse(
        OUTLOOK.replace("DTSTART;TZID=America/Los_Angeles:20261005T090000", "DTSTART;VALUE=DATE:20261005")
    )
    assert day.start == datetime(2026, 10, 5, tzinfo=timezone.utc)


def _mail(calendar, sender="Prasiddha <Prasiddha@acme.com>", dmarc="PASS"):
    return SimpleNamespace(calendar=calendar, from_=sender, verdicts={"dmarc": dmarc}, headers={})


TEAM = {"owner_id": "org_1", "agent_id": "ceo-1", "address": "agents@acme.thelightbulb.company", "verified_senders": []}
UID = "7kukuqrfedlm2f9t0vr42q2gjv@google.com"
CEO, ENG = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
HIRED = "33333333-3333-4333-8333-333333333333"
WITH_DANA = GOOGLE.replace("SEQUENCE:0", "ATTENDEE;CN=Dana:mailto:dana@acme.com\r\nSEQUENCE:0")


_SYNC_BOT = meeting_invites._sync_bot
BEFORE_THE_INVITES = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """The invites above are dated Oct 3-5 2026: the clock reads before them unless a test moves it."""
    monkeypatch.setattr(meeting_invites.time, "time", lambda: BEFORE_THE_INVITES)


@pytest.fixture
def room(monkeypatch):
    """A fake room store: create_room writes META with ``extra``, patch_room_meta merges into it."""
    state = {"meta": None, "lines": {}}

    def post(**kw):  # the repo's receipt: same key + same hash replays, a different hash conflicts
        seen = state["lines"].get(kw["idemp_sk"])
        if seen is not None:
            if seen[0] != kw["req_hash"]:
                raise meeting_invites.messaging_repo.IdempotencyConflict("different request")
            return {"messageId": kw["idemp_sk"]}, True
        state["lines"][kw["idemp_sk"]] = (kw["req_hash"], kw["text"])
        return {"messageId": kw["idemp_sk"]}, False

    def create(**kw):
        state["meta"] = {"roomId": kw["room_id"], **kw["extra"]}

    def patch(room_id, updates, expected_revision=None):  # the repo's CAS on the revision the caller read
        if expected_revision != state["meta"].get("revision", 0):
            raise _cas_lost()
        state["meta"] = {**state["meta"], **updates, "revision": expected_revision + 1}

    def set_if_absent(room_id, field, value, also=None):  # the repo's conditional nested write
        meeting = (state["meta"] or {}).get("meeting")
        if meeting is None or field in meeting:
            return False
        state["meta"] = {**state["meta"], "meeting": {**meeting, field: value, **(also or {})}}
        return True

    m = SimpleNamespace(
        state=state,
        get=AsyncMock(side_effect=lambda room_id: state["meta"]),
        create=AsyncMock(side_effect=create),
        patch=AsyncMock(side_effect=patch),
        post=AsyncMock(side_effect=post),
        accept=AsyncMock(return_value=False),
        members=AsyncMock(
            return_value=[
                {"user_id": "user_p", "email": "Prasiddha@acme.com"},
                {"user_id": "user_d", "email": "dana@acme.com"},
            ]
        ),
    )
    repo = meeting_invites.messaging_repo
    monkeypatch.setattr(repo, "get_room", m.get)
    monkeypatch.setattr(repo, "create_room", m.create)
    monkeypatch.setattr(repo, "patch_room_meta", m.patch)
    monkeypatch.setattr(repo, "set_meeting_field_if_absent", AsyncMock(side_effect=set_if_absent))
    monkeypatch.setattr(repo, "create_message", m.post)
    monkeypatch.setattr(repo, "ensure_user_stream", AsyncMock())
    monkeypatch.setattr(meeting_invites.clerk_admin, "list_organization_members", m.members)
    m.claim = AsyncMock(return_value="tok")
    m.release = AsyncMock()
    monkeypatch.setattr(meeting_invites.container_repo, "try_claim_owner_lifecycle", m.claim)
    monkeypatch.setattr(meeting_invites.container_repo, "release_owner_lifecycle_claim", m.release)
    monkeypatch.setattr(meeting_invites.meeting_rsvp, "accept", m.accept)
    roster = [
        {"id": CEO, "name": "CEO"},
        {"id": ENG, "name": "Engineering"},
        {"id": HIRED, "name": "Hired"},
    ]
    monkeypatch.setattr(meeting_invites.messaging, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(meeting_invites.messaging, "_front_agent_id", AsyncMock(return_value=CEO))
    m.seated = AsyncMock(return_value=[{"id": a["id"]} for a in roster])  # every agent already in the room
    m.upsert = AsyncMock()
    monkeypatch.setattr(repo, "list_members", m.seated)
    monkeypatch.setattr(repo, "upsert_member", m.upsert)
    monkeypatch.setattr(meeting_invites.messaging, "human_display_name", AsyncMock(side_effect=lambda u: u))
    # Meetings available, Recall stubbed out: the `bots` fixture puts the real booking back.
    monkeypatch.setattr(meeting_invites.settings, "LIVEKIT_URL", "wss://lk.test")
    monkeypatch.setattr(meeting_invites.settings, "RECALL_API_KEY", "rk")
    booked = {"recallBotId": "bot-1", "status": "bot_created"}  # returned, not stored: META keeps the invite's status
    m.sync = AsyncMock(side_effect=lambda o, r, meeting, p: {**meeting, **booked})
    monkeypatch.setattr(meeting_invites, "_sync_bot", m.sync)
    m.add_meeting = AsyncMock()
    monkeypatch.setattr(meeting_invites.agent_email_repo, "add_meeting", m.add_meeting)
    return m


def _lines(room):
    return [text for _, text in room.state["lines"].values()]


async def test_a_members_invite_opens_the_meeting_room(room):
    assert await meeting_invites.handle_team_mail("org_1", _mail(WITH_DANA), TEAM) == "meeting_scheduled"
    room.create.assert_awaited_once()
    kw = room.create.await_args.kwargs
    assert kw["room_id"] == meeting_invites.messaging_repo.meeting_room_id("org_1", UID)
    assert kw["kind"] == "channel" and kw["name"] == "Acme weekly sync" and kw["creator_user_id"] == "user_p"
    # Hidden; the inviter is its one human (Dana, a member attendee, is not seated); every agent listens.
    assert kw["extra"]["hidden"] is True
    assert {(m["id"], m["role"], m["listenMode"]) for m in kw["members"]} == {
        ("user_p", "owner", None),
        (CEO, "member", "mentions"),
        (ENG, "member", "mentions"),
        (HIRED, "member", "mentions"),
    }
    meeting = kw["extra"]["meeting"]
    assert meeting["uid"] == UID and meeting["status"] == "scheduled"
    assert "createdBy" not in meeting
    assert meeting["startAt"] == int(datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc).timestamp())
    assert _lines(room) == ["Agents will join **Acme weekly sync** on Sat Oct 3, 3:00 PM EDT."]
    assert all("meet.google.com" not in line for line in _lines(room))
    room.patch.assert_not_awaited()


async def test_a_strangers_invite_schedules_nothing(room):
    # The ICS names a member as ORGANIZER and lists them as an attendee -- both sender-written.
    out = await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE, sender="mallory@evil.example"), TEAM)
    assert out is None  # the route delivers it to the CEO as mail
    room.create.assert_not_awaited()


async def test_an_unauthenticated_member_from_schedules_nothing(room):
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE, dmarc="FAIL"), TEAM) is None
    room.create.assert_not_awaited()


async def test_a_member_forwarding_an_outside_invite_is_scheduled(room):
    cal = GOOGLE.replace("mailto:Prasiddha@acme.com", "mailto:client@outside.example")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_scheduled"
    meeting = room.create.await_args.kwargs["extra"]["meeting"]
    assert meeting["organizer"] == "client@outside.example"
    assert room.create.await_args.kwargs["creator_user_id"] == "user_p"


async def test_plain_team_mail_is_mail(room):
    assert await meeting_invites.handle_team_mail("org_1", _mail(None), TEAM) is None
    room.members.assert_not_awaited()


async def test_a_sequence_bump_moves_the_meeting(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    assert await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM) == "meeting_scheduled"
    room.create.assert_awaited_once()
    meeting = room.patch.await_args.args[1]["meeting"]
    assert meeting["sequence"] == 1
    assert meeting["startAt"] == int(datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc).timestamp())
    assert _lines(room)[-1] == "**Acme weekly sync** moved to Sat Oct 3, 4:00 PM EDT."


async def test_cancel_marks_the_room_cancelled(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    cal = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_cancelled"
    assert room.state["meta"]["meeting"]["status"] == "cancelled"
    assert _lines(room)[-1] == "**Acme weekly sync** was cancelled."


async def test_a_cancel_without_a_room_is_ignored(room):
    cal = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_ignored"
    room.create.assert_not_awaited()


async def test_an_older_sequence_does_not_overwrite(room):
    room.state["meta"] = {"meeting": {"sequence": 3, "organizer": "prasiddha@acme.com", "status": "scheduled"}}
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_stale"
    room.patch.assert_not_awaited()
    room.post.assert_not_awaited()


async def test_a_resend_after_a_move_neither_raises_nor_doubles_the_line(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM)
    # Google resends on a description edit with no SEQUENCE bump: same key, different wording.
    resent = moved.replace("Weekly sync", "Weekly sync, agenda attached")
    assert await meeting_invites.handle_team_mail("org_1", _mail(resent), TEAM) == "meeting_scheduled"
    assert len(_lines(room)) == 2


def _cas_lost():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")


async def test_a_lost_meta_race_is_retried_once(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.patch.side_effect = [_cas_lost(), None]
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    assert await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM) == "meeting_scheduled"
    assert room.patch.await_count == 2
    room.patch.side_effect = [_cas_lost(), _cas_lost(), _cas_lost()]
    again = moved.replace("SEQUENCE:1", "SEQUENCE:2")
    with pytest.raises(meeting_invites.ClientError):  # three lost races: SES redelivers
        await meeting_invites.handle_team_mail("org_1", _mail(again), TEAM)
    assert room.patch.await_count == 5


async def test_a_lost_race_to_a_newer_sequence_is_not_retried(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)

    def newer_won(room_id, updates, **_):
        room.state["meta"] = {**room.state["meta"], "meeting": {**room.state["meta"]["meeting"], "sequence": 5}}
        raise _cas_lost()

    room.patch.side_effect = newer_won
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    assert await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM) == "meeting_stale"
    assert room.patch.await_count == 1
    assert room.state["meta"]["meeting"]["sequence"] == 5


async def test_an_older_sequence_that_read_the_room_first_writes_nothing(room):
    room.accept.return_value = True
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    stale_view = room.state["meta"]
    room.state["meta"] = {**stale_view, "meeting": {**stale_view["meeting"], "sequence": 3}}  # SEQUENCE 3 committed
    views = iter([stale_view])  # SEQUENCE 2 read the room before it did
    room.get.side_effect = lambda room_id: next(views, room.state["meta"])
    lines, patches = _lines(room), room.patch.await_count
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:2").replace("T150000", "T160000")
    assert await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM) == "meeting_stale"
    assert room.patch.await_count == patches and _lines(room) == lines
    room.accept.assert_awaited_once()
    assert room.state["meta"]["meeting"]["sequence"] == 3


async def test_a_stale_patch_writes_nothing(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.state["meta"]["meeting"]["sequence"] = 3
    assert await meeting_invites._patch_meeting(room.state["meta"]["roomId"], {"sequence": 2}) is None
    assert room.state["meta"]["meeting"]["sequence"] == 3


async def test_a_racing_second_creator_falls_through_to_the_update(room):
    def lost_race(**kw):
        room.state["meta"] = {"roomId": kw["room_id"], **kw["extra"]}  # the other member's create landed
        raise meeting_invites.messaging_repo.MessagingRepoError("room create cancelled without a durable receipt")

    room.create.side_effect = lost_race
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    assert _lines(room) == ["Agents will join **Acme weekly sync** on Sat Oct 3, 3:00 PM EDT."]


async def test_a_cancelled_create_that_left_no_room_propagates(room):
    room.create.side_effect = meeting_invites.messaging_repo.MessagingRepoError("cancelled, no receipt")
    with pytest.raises(meeting_invites.messaging_repo.MessagingRepoError):
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert _lines(room) == []


async def test_a_throttle_on_the_retry_propagates(room):
    from botocore.exceptions import ClientError

    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    throttle = ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems")
    room.patch.side_effect = [_cas_lost(), throttle]
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    with pytest.raises(ClientError):
        await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM)


async def test_a_reinvite_after_cancel_announces_itself_again(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    cancel = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cancel), TEAM) == "meeting_cancelled"
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    join = "Agents will join **Acme weekly sync** on Sat Oct 3, 3:00 PM EDT."
    assert _lines(room) == [join, "**Acme weekly sync** was cancelled.", join]
    assert room.state["meta"]["meeting"]["status"] == "scheduled"


async def test_only_a_scheduled_meeting_is_accepted(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE, sender="mallory@evil.example"), TEAM)
    room.accept.assert_not_awaited()
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    room.accept.assert_awaited_once()
    assert room.accept.await_args.args[0] == "org_1" and room.accept.await_args.args[1].uid == UID
    assert "rsvpSequence" not in room.state["meta"]["meeting"]  # a failed RSVP is not recorded
    cal = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_cancelled"
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_stale"
    room.accept.assert_awaited_once()


async def test_an_rsvp_is_sent_once_per_sequence(room):
    room.accept.return_value = True
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert room.state["meta"]["meeting"]["rsvpSequence"] == 0
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.accept.assert_awaited_once()
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1")), TEAM)
    assert room.accept.await_count == 2 and room.state["meta"]["meeting"]["rsvpSequence"] == 1


@pytest.mark.parametrize("method", ["PUBLISH", "COUNTER", "REFRESH", "REPLY"])
def test_non_invite_methods_are_none(method):
    assert meeting_invites.parse(GOOGLE.replace("METHOD:REQUEST", f"METHOD:{method}")) is None


async def test_a_cancel_for_an_archived_room_is_ignored(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.state["meta"]["archivedAt"] = "2026-10-03T00:00:00Z"
    room.post.side_effect = meeting_invites.messaging_repo.RoomArchived("r")  # what the real repo would do
    cancel = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cancel), TEAM) == "meeting_ignored"
    assert _lines(room) == ["Agents will join **Acme weekly sync** on Sat Oct 3, 3:00 PM EDT."]
    assert room.state["meta"]["meeting"]["status"] == "scheduled"


async def test_a_paused_team_row_sends_no_rsvp(room):
    room.accept.return_value = True
    paused = {**TEAM, "outbound_disabled": "complaint"}
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), paused) == "meeting_scheduled"
    room.accept.assert_not_awaited()
    assert "rsvpSequence" not in room.state["meta"]["meeting"]


async def test_a_clerk_failure_propagates(room):
    room.members.side_effect = RuntimeError("clerk 502")
    with pytest.raises(RuntimeError):
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert room.members.await_args.kwargs == {"strict": True}


@pytest.fixture
def bots(room, monkeypatch):
    """Recall faked at the adapter; the real booking path."""
    b = SimpleNamespace(
        create=AsyncMock(return_value="bot-1"),
        find=AsyncMock(return_value=None),
        audio=AsyncMock(return_value="separate"),
        reschedule=AsyncMock(),
        leave=AsyncMock(),
        metric=[],
    )
    monkeypatch.setattr(meeting_invites, "_sync_bot", _SYNC_BOT)
    monkeypatch.setattr(meeting_invites.settings, "RECALL_API_KEY", "rk")
    monkeypatch.setattr(meeting_invites.meeting_bots, "create", b.create)
    monkeypatch.setattr(meeting_invites.meeting_bots, "find_for_room", b.find)
    monkeypatch.setattr(meeting_invites.meeting_bots, "audio_mode", b.audio)
    monkeypatch.setattr(meeting_invites.meeting_bots, "reschedule", b.reschedule)
    monkeypatch.setattr(meeting_invites.meeting_bots, "leave", b.leave)
    monkeypatch.setattr(meeting_invites.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr(meeting_invites, "put_metric", lambda name, **kw: b.metric.append((name, kw)))
    return b


MOVED = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
CANCEL = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")


async def test_an_invite_books_one_bot(room, bots):
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    bots.create.assert_awaited_once()
    kw = bots.create.await_args.kwargs
    assert kw["company"] == "Acme" and kw["room_id"] == room.state["meta"]["roomId"]
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_created" and meeting["recallBotId"] == "bot-1"
    assert meeting["ticketNonce"] == kw["meeting"]["ticketNonce"]  # persisted before the POST
    bots.find.assert_not_awaited()  # a nonce claimed just now has no bot to adopt: no List Bots call
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)  # SES retry / resend
    bots.create.assert_awaited_once()


@pytest.mark.parametrize("missing", ["LIVEKIT_URL", "RECALL_API_KEY"])
async def test_meetings_unavailable_keep_the_room_for_notes(room, bots, monkeypatch, missing):
    monkeypatch.setattr(meeting_invites.settings, missing, "")
    room.accept.return_value = True
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    bots.create.assert_not_awaited()
    room.accept.assert_not_awaited()
    assert room.state["meta"]["meeting"]["status"] == "scheduled"
    assert _lines(room) == [
        "Meetings aren't available in this environment yet, so no agents will join **Acme weekly sync**."
    ]


async def test_a_full_bot_pool_is_bot_failed_not_a_raise(room, bots):
    bots.create.side_effect = meeting_invites.meeting_bots.RecallError("507")
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_failed" and meeting["botError"] == "507"
    assert "recallBotId" not in meeting and meeting["ticketNonce"]  # a 5xx may have booked: the key stays
    assert bots.metric == [("meeting.bot_failed", {"dimensions": {"code": "507"}})]
    assert _lines(room)[-1] == (
        "Couldn't book a bot for **Acme weekly sync**: no meeting bots are free right now. "
        "Forward the invite again to retry."
    )


async def test_a_resend_on_bot_failed_retries_create(room, bots):
    bots.create.side_effect = [meeting_invites.meeting_bots.RecallError("507"), "bot-2"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert bots.create.await_count == 2
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_created" and meeting["recallBotId"] == "bot-2" and "botError" not in meeting


async def test_a_move_reschedules_the_bot_once(room, bots):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.state["meta"]["meeting"].pop("audio")  # a bot booked before per-person audio
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
    bots.create.assert_awaited_once()
    bots.reschedule.assert_awaited_once()
    assert room.state["meta"]["meeting"]["audio"] == "separate"  # the move re-sent the recording config
    assert bots.reschedule.await_args.args == ("bot-1",)
    assert bots.reschedule.await_args.kwargs["meeting"]["startAt"] == int(
        datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc).timestamp()
    )
    assert room.state["meta"]["meeting"]["status"] == "bot_created"


async def test_a_move_too_close_to_start_replaces_the_bot(room, bots):
    bots.create.side_effect = ["bot-1", "bot-2"]
    bots.reschedule.side_effect = meeting_invites.meeting_bots.RecallError("400")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
    bots.leave.assert_awaited_once_with("bot-1")
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-2"


async def test_cancel_pulls_the_bot_once(room, bots):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert await meeting_invites.handle_team_mail("org_1", _mail(CANCEL), TEAM) == "meeting_cancelled"
    bots.leave.assert_awaited_once_with("bot-1")
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "cancelled" and "recallBotId" not in meeting
    bots.create.assert_awaited_once()


def _bot_lines(room):
    return [line for line in _lines(room) if "bot" in line]


async def test_two_deliveries_book_one_bot(room, bots):
    nonces = []

    async def create(**kw):
        nonces.append(kw["meeting"]["ticketNonce"])
        if len(nonces) == 1:  # a second SES delivery lands while the first POST is in flight
            await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
        return "bot-1"

    bots.create.side_effect = create
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert len(nonces) == 1  # the second delivery saw a fresh claim and left the POST to it
    assert room.state["meta"]["meeting"]["ticketNonce"] == nonces[0]


async def test_a_bot_id_survives_two_lost_meta_races(room, bots):
    merge, fails = room.patch.side_effect, [_cas_lost(), _cas_lost()]

    def flaky(room_id, updates, **kw):
        if fails:
            raise fails.pop(0)
        merge(room_id, updates, **kw)

    room.patch.side_effect = flaky
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-1"


async def test_a_bot_write_losing_three_times_raises_for_ses(room, bots):
    room.patch.side_effect = _cas_lost()
    with pytest.raises(meeting_invites.ClientError):
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)


async def test_a_move_that_recall_drops_keeps_the_old_bot(room, bots):
    bots.reschedule.side_effect = meeting_invites.meeting_bots.RecallError("503")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_failed" and meeting["botError"] == "503" and meeting["recallBotId"] == "bot-1"
    bots.leave.assert_not_awaited()
    bots.create.side_effect = ["bot-2"]
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)  # forwarded again
    bots.leave.assert_awaited_once_with("bot-1")
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-2"


async def test_a_replacement_whose_leave_fails_keeps_the_old_bot(room, bots):
    bots.reschedule.side_effect = meeting_invites.meeting_bots.RecallError("400")
    bots.leave.side_effect = meeting_invites.meeting_bots.RecallError("503")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
    bots.create.assert_awaited_once()
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-1"
    assert _bot_lines(room) == ["Couldn't move the bot for **Acme weekly sync**; it will join at the old time."]


async def test_a_new_meet_link_replaces_the_bot(room, bots):
    bots.create.side_effect = ["bot-1", "bot-2"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    first = room.state["meta"]["meeting"]["ticketNonce"]
    relinked = GOOGLE.replace("abc-defg-hij", "xyz-wxyz-xyz").replace("SEQUENCE:0", "SEQUENCE:1")
    await meeting_invites.handle_team_mail("org_1", _mail(relinked), TEAM)
    bots.leave.assert_awaited_once_with("bot-1")
    bots.reschedule.assert_not_awaited()
    meeting = room.state["meta"]["meeting"]
    assert meeting["recallBotId"] == "bot-2" and meeting["ticketNonce"] != first
    assert bots.create.await_args.kwargs["meeting"]["meetingUrl"] == "https://meet.google.com/xyz-wxyz-xyz"


async def test_a_cancel_whose_leave_fails_says_so_and_retries(room, bots):
    bots.leave.side_effect = [meeting_invites.meeting_bots.RecallError("503"), None]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(CANCEL), TEAM)
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-1"
    assert _lines(room)[-1] == "Couldn't pull the bot out of **Acme weekly sync**."
    await meeting_invites.handle_team_mail("org_1", _mail(CANCEL), TEAM)
    assert bots.leave.await_count == 2 and "recallBotId" not in room.state["meta"]["meeting"]


async def test_an_unreadable_recall_answer_is_bot_failed(room, bots):
    bots.create.side_effect = meeting_invites.meeting_bots.RecallError("bad_response")
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    assert room.state["meta"]["meeting"]["status"] == "bot_failed"
    assert _bot_lines(room)[-1].startswith("Couldn't book a bot for **Acme weekly sync**: the meeting-bot service sent")


async def test_a_definitive_4xx_mints_a_fresh_nonce_on_retry(room, bots):
    bots.create.side_effect = [meeting_invites.meeting_bots.RecallError("400"), "bot-2"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert "ticketNonce" not in room.state["meta"]["meeting"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    first, second = (c.kwargs["meeting"]["ticketNonce"] for c in bots.create.await_args_list)
    assert first != second and room.state["meta"]["meeting"]["recallBotId"] == "bot-2"


async def test_a_409_from_recall_is_never_acknowledged_without_a_bot(room, bots):
    """The first POST timed out here but is still in flight at Recall: the retry's 409 raises, so
    the SES route 5xx's, and the redelivery reuses the stored nonce and gets the cached bot."""
    err = meeting_invites.meeting_bots.RecallError
    bots.create.side_effect = [err("timeout"), err("409"), "bot-1"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    with pytest.raises(meeting_invites.MeetingBusy):
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert "recallBotId" not in room.state["meta"]["meeting"]
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    nonces = {c.kwargs["meeting"]["ticketNonce"] for c in bots.create.await_args_list}
    assert len(nonces) == 1 and room.state["meta"]["meeting"]["recallBotId"] == "bot-1"


async def test_a_timeout_retry_reuses_the_nonce(room, bots):
    bots.create.side_effect = [meeting_invites.meeting_bots.RecallError("timeout"), "bot-1"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    first, second = (c.kwargs["meeting"]["ticketNonce"] for c in bots.create.await_args_list)
    assert first == second


async def test_two_first_deliveries_racing_the_room_create_book_one_bot(room, bots):
    """B misses the room, A creates it and claims the nonce mid-POST, then B's create loses and
    re-reads: B must neither erase A's claim nor POST."""
    calls = []

    async def create(**kw):
        calls.append(kw["meeting"]["ticketNonce"])
        if len(calls) == 1:  # B arrives during A's POST, having read no room
            seen = {"n": 0}

            def get(room_id):
                seen["n"] += 1
                return None if seen["n"] == 1 else room.state["meta"]

            room.get.side_effect = get
            room.create.side_effect = meeting_invites.messaging_repo.MessagingRepoError("lost the create")
            await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
        return "bot-1"

    bots.create.side_effect = create
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert len(calls) == 1 and meeting["ticketNonce"] == calls[0] and meeting["recallBotId"] == "bot-1"


async def test_the_rsvp_write_keeps_the_bot(room, bots):
    room.accept.return_value = True
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert meeting["rsvpSequence"] == 0 and meeting["recallBotId"] == "bot-1" and meeting["ticketNonce"]
    assert meeting["audio"] == "separate"  # the page asks for it: one LiveKit seat per Meet person


def test_the_audio_mode_goes_with_the_bot():
    # A new occurrence (or a replaced bot) is a new booking; its mode is written again with it.
    assert "audio" in meeting_invites._BOT_KEYS and "audio" in meeting_invites._OCCURRENCE_KEYS


async def test_a_winner_timing_out_after_the_other_delivery_booked_is_not_a_failure(room, bots):
    async def create(**kw):  # the POST times out, but the bot it booked was written meanwhile
        meta = room.state["meta"]
        room.state["meta"] = {**meta, "meeting": {**meta["meeting"], "recallBotId": "bot-1", "status": "bot_created"}}
        raise meeting_invites.meeting_bots.RecallError("timeout")

    bots.create.side_effect = create
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-1"
    assert _bot_lines(room) == []


async def test_a_cancel_during_the_booking_pulls_the_new_bot(room, bots):
    async def create(**kw):  # the CANCEL lands while the POST is in flight
        assert await meeting_invites.handle_team_mail("org_1", _mail(CANCEL), TEAM) == "meeting_cancelled"
        return "bot-1"

    bots.create.side_effect = create
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    bots.leave.assert_awaited_once_with("bot-1")
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "cancelled" and "recallBotId" not in meeting


async def test_a_move_during_the_booking_reschedules_the_new_bot(room, bots):
    async def create(**kw):  # the move lands while the POST is in flight
        await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
        return "bot-1"

    bots.create.side_effect = create
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    bots.create.assert_awaited_once()
    bots.reschedule.assert_awaited_once()
    assert bots.reschedule.await_args.args == ("bot-1",)
    assert bots.reschedule.await_args.kwargs["meeting"]["startAt"] == int(
        datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc).timestamp()
    )
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_created" and meeting["recallBotId"] == "bot-1"


async def test_a_delivery_while_the_meeting_lease_is_held_retries_then_sees_the_newer_sequence(room):
    room.claim.return_value = None  # another delivery (the SEQUENCE:1 move) holds the lease
    with pytest.raises(meeting_invites.MeetingBusy):  # the SES route 503s; SES redelivers
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.get.assert_not_awaited()
    room.release.assert_not_awaited()
    # The holder committed sequence 1 and released; the redelivery of sequence 0 is stale.
    room.state["meta"] = {"meeting": {"sequence": 1, "organizer": "prasiddha@acme.com", "status": "scheduled"}}
    room.claim.return_value = "tok"
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_stale"
    assert room.claim.await_args.kwargs == {"stale_s": 60}
    room.release.assert_awaited_once_with(room.claim.await_args.args[0], token="tok")
    room.post.assert_not_awaited()
    room.patch.assert_not_awaited()
    room.accept.assert_not_awaited()


async def test_the_meeting_lease_is_released_when_the_handler_raises(room):
    room.members.side_effect = None
    room.post.side_effect = RuntimeError("ddb")
    with pytest.raises(RuntimeError):
        await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.release.assert_awaited_once()


def test_a_multi_address_organizer_is_dropped():
    cal = GOOGLE.replace("mailto:Prasiddha@acme.com", "mailto:a@x.com,b@y.com")
    assert meeting_invites.parse(cal).organizer_email is None


async def test_a_write_is_bound_to_the_revision_it_read(room):
    """A transcript write that read ``in_call`` must not put it back over a webhook's ``ended``."""
    room.state["meta"] = {"roomId": "r", "revision": 1, "meeting": {"status": "in_call"}}
    reads = iter([dict(room.state["meta"])])  # A's first read, then B commits before A writes

    def get(room_id):
        stale = next(reads, None)
        if stale is not None:
            room.state["meta"] = {"roomId": "r", "revision": 2, "meeting": {"status": "ended"}}
            return stale
        return room.state["meta"]

    room.get.side_effect = get
    written = await meeting_invites._patch_meeting("r", {"transcriptAttachedAt": 5})
    assert written == room.state["meta"]["meeting"] == {"status": "ended", "transcriptAttachedAt": 5}
    assert [c.kwargs["expected_revision"] for c in room.patch.await_args_list] == [1, 2]


async def test_a_function_of_the_map_is_redone_over_the_racing_write(room):
    """A read-modify-write (the roster) applied over the lost race's state, not the first read."""
    room.state["meta"] = {"roomId": "r", "revision": 1, "meeting": {"roster": {"1": "Pat"}}}
    reads = iter([dict(room.state["meta"])])

    def get(room_id):
        stale = next(reads, None)
        if stale is not None:
            room.state["meta"] = {"roomId": "r", "revision": 2, "meeting": {"roster": {"1": "Pat", "2": "Dan"}}}
            return stale
        return room.state["meta"]

    room.get.side_effect = get
    written = await meeting_invites._patch_meeting("r", lambda m: {"roster": {**m["roster"], "3": "Lee"}})
    assert written["roster"] == {"1": "Pat", "2": "Dan", "3": "Lee"}


async def test_a_new_meeting_and_every_written_change_nudge_the_mail_panel(room):
    nudge = meeting_invites.agent_email.mail_changed
    order: list[str] = []
    room.add_meeting.side_effect = lambda *_: order.append("listed")
    nudge.side_effect = lambda *_: order.append("nudged")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    nudge.assert_awaited_with("org_1")  # the record's creation
    assert order[-1] == "nudged" and "listed" in order  # the last nudge follows the listing write
    nudge.side_effect = None
    nudge.reset_mock()
    room.state["meta"]["ownerId"] = "org_1"
    await meeting_invites._patch_meeting(room.state["meta"]["roomId"], {"status": "in_call"})
    nudge.assert_awaited_once_with("org_1")
    nudge.reset_mock()
    await meeting_invites._patch_meeting(room.state["meta"]["roomId"], {"status": "in_call"})  # nothing changes
    nudge.assert_not_awaited()


NO_START = "DTSTART;TZID=America/New_York:20261003T150000\r\n"


def test_a_cancel_needs_no_dtstart_but_a_request_does():
    assert meeting_invites.parse(GOOGLE.replace(NO_START, "")) is None
    for cancel in ("METHOD:CANCEL", "METHOD:REQUEST\r\nX-NOTE:x"):
        cal = (
            GOOGLE.replace(NO_START, "")
            .replace("METHOD:REQUEST", cancel)
            .replace("STATUS:CONFIRMED", "STATUS:CANCELLED")
        )
        inv = meeting_invites.parse(cal)
        assert inv.method == "CANCEL" and inv.start is None


async def test_a_cancel_without_dtstart_cancels_the_stored_meeting(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    cal = GOOGLE.replace(NO_START, "").replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_cancelled"
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "cancelled" and meeting["startAt"]
    assert _lines(room)[-1] == "**Acme weekly sync** was cancelled."


async def test_no_rsvp_unless_a_bot_is_booked(room):
    room.accept.return_value = True
    room.sync.side_effect = lambda o, r, meeting, p: {**meeting, "status": "bot_failed", "recallBotId": "bot-1"}
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    room.sync.side_effect = lambda o, r, meeting, p: meeting  # nothing booked at all
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1")), TEAM)
    room.accept.assert_not_awaited()
    assert "rsvpSequence" not in room.state["meta"]["meeting"]


def test_tzid_parameter_name_is_case_insensitive():
    lower = OUTLOOK.replace("DTSTART;TZID=", "DTSTART;tzid=")
    assert meeting_invites.parse(lower).start == meeting_invites.parse(OUTLOOK).start


async def test_a_replayed_cancel_posts_no_second_line(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    cal = GOOGLE.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:1")
    await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM)
    assert _lines(room).count("**Acme weekly sync** was cancelled.") == 1


# --- a meeting is a workspace call: no channel, every agent, output in the default room, series ---

NY = __import__("zoneinfo").ZoneInfo("America/New_York")
SERIES = GOOGLE.replace("SEQUENCE:0", "RRULE:FREQ=WEEKLY;BYDAY=SA\r\nSEQUENCE:0")
ONE_OCCURRENCE = GOOGLE.replace("SEQUENCE:0", "RECURRENCE-ID;TZID=America/New_York:20261010T150000\r\nSEQUENCE:0")


def _epoch(*args):
    return int(datetime(*args, tzinfo=NY).timestamp())


@pytest.fixture
def now(monkeypatch):
    clock = {"t": _epoch(2026, 11, 5, 12, 0)}  # a Thursday, after DST ended on Nov 1
    monkeypatch.setattr(meeting_invites.time, "time", lambda: clock["t"])
    return clock


def test_a_series_is_dated_to_its_next_occurrence_on_the_same_wall_clock(now):
    inv = meeting_invites.parse(SERIES)
    assert inv.start == datetime(2026, 11, 7, 15, 0, tzinfo=NY)  # 3 PM EST, as 3 PM EDT was
    assert int(inv.start.timestamp()) == int(datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc).timestamp())
    assert inv.end - inv.start == __import__("datetime").timedelta(minutes=30)
    assert inv.recurrence == {
        "rule": "FREQ=WEEKLY;BYDAY=SA",
        "first": _epoch(2026, 10, 3, 15, 0),
        "tz": "America/New_York",
        "exdates": [],
        "duration": 1800,
    }


def test_an_exdate_skips_its_occurrence(now):
    cal = SERIES.replace("SEQUENCE:0", "EXDATE;TZID=America/New_York:20261031T150000,20261107T150000\r\nSEQUENCE:0")
    inv = meeting_invites.parse(cal)
    assert inv.start == datetime(2026, 11, 14, 15, 0, tzinfo=NY)
    assert inv.recurrence["exdates"] == [_epoch(2026, 11, 7, 15, 0)]  # Oct 31 is past: not stored


def test_an_occurrence_in_progress_is_the_next_one(now):
    now["t"] = _epoch(2026, 11, 7, 15, 10)
    assert meeting_invites.parse(SERIES).start == datetime(2026, 11, 7, 15, 0, tzinfo=NY)


def test_a_finished_series_is_a_cancel(now):
    inv = meeting_invites.parse(SERIES.replace("BYDAY=SA", "BYDAY=SA;COUNT=2"))
    assert inv.method == "CANCEL" and inv.start is None and inv.recurrence is None


def test_the_master_wins_over_an_edited_occurrence(now):
    edited = ONE_OCCURRENCE.replace("SUMMARY:Acme weekly sync", "SUMMARY:Edited one")
    events = edited[edited.index("BEGIN:VEVENT") : edited.index("END:VCALENDAR")]
    cal = SERIES.replace("END:VCALENDAR", events + "END:VCALENDAR")  # Google sends the master, then exceptions
    inv = meeting_invites.parse(cal.replace("BEGIN:VEVENT", events + "BEGIN:VEVENT", 1))  # and either order
    assert inv.uid == UID and inv.summary == "Acme weekly sync" and inv.recurrence and inv.instance_of is None


def test_a_lone_occurrence_is_a_one_off_of_its_own():
    inv = meeting_invites.parse(ONE_OCCURRENCE)
    assert inv.uid == f"{UID}#{_epoch(2026, 10, 10, 15, 0)}" and inv.instance_of == UID and inv.recurrence is None


async def test_one_occurrence_of_a_followed_series_is_ignored(room):
    room.state["meta"] = {"roomId": "series", "meeting": {"uid": UID, "status": "scheduled"}}
    assert await meeting_invites.handle_team_mail("org_1", _mail(ONE_OCCURRENCE), TEAM) == "meeting_ignored"
    room.create.assert_not_awaited()


async def test_one_occurrence_alone_books_its_own_meeting(room):
    reads = []

    def get(room_id):
        reads.append(room_id)
        return room.state["meta"]

    room.get.side_effect = get
    assert await meeting_invites.handle_team_mail("org_1", _mail(ONE_OCCURRENCE), TEAM) == "meeting_scheduled"
    assert reads[0] == meeting_invites.messaging_repo.meeting_room_id("org_1", UID)  # the series looked up first
    kw = room.create.await_args.kwargs
    assert kw["room_id"] == meeting_invites.messaging_repo.meeting_room_id(
        "org_1", f"{UID}#{_epoch(2026, 10, 10, 15, 0)}"
    )


async def test_a_series_invite_stores_its_rule_and_says_so(room, now):
    assert await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM) == "meeting_scheduled"
    meeting = room.state["meta"]["meeting"]
    assert meeting["recurrence"]["rule"] == "FREQ=WEEKLY;BYDAY=SA"
    assert meeting["startAt"] == _epoch(2026, 11, 7, 15, 0) and meeting["endAt"] == _epoch(2026, 11, 7, 15, 30)
    assert _lines(room) == ["Agents will join **Acme weekly sync** each time it meets. Next: Sat Nov 7, 3:00 PM EST."]
    # The organizer turns the series into a one-off: the rule goes.
    single = GOOGLE.replace("20261003T", "20261120T").replace("SEQUENCE:0", "SEQUENCE:1")
    await meeting_invites.handle_team_mail("org_1", _mail(single), TEAM)
    assert "recurrence" not in room.state["meta"]["meeting"]


async def test_a_re_invite_after_the_meeting_ended_is_not_a_move(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    room.state["meta"]["meeting"]["status"] = "ended"
    await meeting_invites.handle_team_mail("org_1", _mail(MOVED), TEAM)
    assert not any("moved" in line for line in _lines(room))


async def test_lines_go_to_the_default_room_else_the_meetings_own(room):
    default = meeting_invites.messaging.default_room_id("org_1")
    meeting_room = meeting_invites.messaging_repo.meeting_room_id("org_1", UID)
    rooms = {default: {"roomId": default}}
    room.get.side_effect = lambda room_id: rooms.get(room_id) if room_id == default else room.state["meta"]
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert room.post.await_args.kwargs["room_id"] == default
    assert room.post.await_args.kwargs["idemp_pk"] == meeting_invites.messaging_repo.room_pk(default)
    rooms[default]["archivedAt"] = "2026-10-03T00:00:00Z"
    assert await meeting_invites.output_room("org_1", meeting_room) == meeting_room
    rooms.clear()
    assert await meeting_invites.output_room("org_1", meeting_room) == meeting_room


def _ended_series(room, **extra):
    room.state["meta"] = {
        "roomId": "r",
        "ownerId": "org_1",
        "revision": 0,
        "meeting": {
            "uid": UID,
            "sequence": 0,
            "title": "Acme weekly sync",
            "status": "ended",
            "startAt": _epoch(2026, 11, 7, 15, 0),
            "endAt": _epoch(2026, 11, 7, 15, 30),
            "meetingUrl": "https://meet.google.com/abc-defg-hij",
            "recurrence": meeting_invites.parse(SERIES).recurrence,
            **{k: 1 for k in meeting_invites._OCCURRENCE_KEYS},
            **extra,
        },
    }


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(meeting_invites, "put_metric", lambda name, **kw: seen.append((name, kw)))
    return seen


async def test_advance_books_the_next_occurrence(room, now, metrics):
    _ended_series(room)
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "scheduled"
    assert meeting["startAt"] == _epoch(2026, 11, 14, 15, 0) and meeting["endAt"] == _epoch(2026, 11, 14, 15, 30)
    assert not set(meeting_invites._OCCURRENCE_KEYS) & set(meeting)
    owner, room_id, written, prior = room.sync.await_args.args
    assert (owner, room_id, written) == ("org_1", "r", meeting) and prior["recallBotId"] == 1
    assert metrics == [("meeting.advance", {"dimensions": {"outcome": "advanced"}})]
    await meeting_invites.advance("r")  # a second trigger: the next occurrence is not over yet
    assert room.sync.await_count == 1 and metrics[-1][1]["dimensions"]["outcome"] == "not_due"


async def test_advance_of_an_early_end_skips_the_current_occurrence(room, now, metrics):
    _ended_series(room)
    now["t"] = _epoch(2026, 11, 7, 15, 5)  # the idle leave ended it 25 minutes early
    await meeting_invites.advance("r")
    assert room.state["meta"]["meeting"]["startAt"] == _epoch(2026, 11, 14, 15, 0)


async def test_advance_at_the_end_of_a_series(room, now, metrics):
    _ended_series(room, recurrence={**meeting_invites.parse(SERIES).recurrence, "rule": "FREQ=WEEKLY;COUNT=6"})
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    assert room.state["meta"]["meeting"]["status"] == "ended"
    room.sync.assert_not_awaited()
    assert metrics == [("meeting.advance", {"dimensions": {"outcome": "series_over"}})]


async def test_advance_retries_a_busy_lease_then_gives_up(room, now, metrics, monkeypatch):
    _ended_series(room)
    sleep = AsyncMock()
    monkeypatch.setattr(meeting_invites.asyncio, "sleep", sleep)
    room.claim.return_value = None
    await meeting_invites.advance("r")
    assert room.claim.await_count == 4 and [c.args for c in sleep.await_args_list] == [(2,)] * 3
    assert room.state["meta"]["meeting"]["status"] == "ended"
    assert metrics == [("meeting.advance", {"dimensions": {"outcome": "busy"}})]
    room.claim.side_effect = [None, "tok"]  # held once, then free
    await meeting_invites.advance("r")
    assert room.state["meta"]["meeting"]["status"] == "scheduled"


async def test_advance_leaves_one_offs_and_live_meetings_alone(room, metrics):
    _ended_series(room, recurrence=None)
    await meeting_invites.advance("r")
    room.claim.assert_not_awaited()  # a one-off takes no lease
    _ended_series(room, status="in_call")
    await meeting_invites.advance("r")
    room.sync.assert_not_awaited()
    assert [m[1]["dimensions"]["outcome"] for m in metrics] == ["not_series", "not_due"]


async def test_advance_never_raises(room, metrics):
    _ended_series(room)
    room.get.side_effect = RuntimeError("ddb")
    await meeting_invites.advance("r")
    assert metrics == [("meeting.advance", {"dimensions": {"outcome": "error"}})]


async def test_a_hidden_room_is_in_no_room_list_but_an_old_meeting_room_stays(table, monkeypatch):  # noqa: F811
    repo, messaging = meeting_invites.messaging_repo, meeting_invites.messaging
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    members = [_member("human", USER_A, "owner")]
    for client, extra in (("hidden", {"meeting": {"uid": "a"}, "hidden": True}), ("old", {"meeting": {"uid": "b"}})):
        await repo.create_room(
            owner_id=OWNER,
            room_id=repo.deterministic_room_id(OWNER, USER_A, client),
            client_room_id=client,
            creator_user_id=USER_A,
            name=client,
            kind="channel",
            members=members,
            req_hash=repo.request_hash(client),
            extra=extra,
        )
    listed = await messaging.list_rooms(AuthContext(user_id=USER_A, org_id=OWNER))
    assert [r.name for r in listed.rooms] == ["old"]


# --- fix round 1 ---


async def test_the_bridges_late_answer_in_a_hidden_room_lands_in_the_default_room_once(table, monkeypatch):  # noqa: F811
    """The exact body bridge/turn.py _post_late_call_answer POSTs to /internal/messaging/agent-messages."""
    repo, messaging = meeting_invites.messaging_repo, meeting_invites.messaging
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": FRIDAY, "name": "Friday"}]))
    monkeypatch.setattr(messaging, "_emit_reply_telemetry", AsyncMock())
    members = [_member("human", USER_A, "owner"), _member("agent", FRIDAY, listen="mentions", handle="friday")]
    default, hidden = messaging.default_room_id(OWNER), repo.deterministic_room_id(OWNER, USER_A, "meet")
    for room_id, extra in ((default, {}), (hidden, {"meeting": {"uid": "a"}, "hidden": True})):
        await repo.create_room(
            owner_id=OWNER,
            room_id=room_id,
            client_room_id=room_id,
            creator_user_id=USER_A,
            name="r",
            kind="channel",
            members=members,
            req_hash=repo.request_hash(room_id),
            extra=extra,
        )
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "live-late:run-9",
            "roomId": hidden,
            "agentId": FRIDAY,
            "text": "From the Live call: three tasks open.",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    first = await messaging.create_agent_message(OWNER, request)
    again = await messaging.create_agent_message(OWNER, request)  # the bridge's retry: same room, a replay
    assert str(first.room_id) == default and again.message_id == first.message_id
    assert (await repo.get_room(default))["lastSeq"] == 1 and (await repo.get_room(hidden))["lastSeq"] == 0


def test_hourly_and_ancient_series_are_mail(now):
    assert meeting_invites.parse(SERIES.replace("FREQ=WEEKLY;BYDAY=SA", "FREQ=HOURLY")) is None
    old = SERIES.replace("DTSTART;TZID=America/New_York:20261003", "DTSTART;TZID=America/New_York:19961005")
    assert (
        meeting_invites.parse(
            old.replace("DTEND;TZID=America/New_York:20261003", "DTEND;TZID=America/New_York:19961005")
        )
        is None
    )


def test_exdates_and_duration_are_bounded(now):
    many = ",".join(f"2027{m:02d}{d:02d}T150000" for m in range(1, 13) for d in range(1, 29))  # 336 future dates
    inv = meeting_invites.parse(SERIES.replace("SEQUENCE:0", f"EXDATE;TZID=America/New_York:{many}\r\nSEQUENCE:0"))
    assert len(inv.recurrence["exdates"]) == 200
    long = SERIES.replace("DTEND;TZID=America/New_York:20261003T153000", "DTEND;TZID=America/New_York:20261010T153000")
    assert meeting_invites.parse(long).recurrence["duration"] == 86400


def test_a_utc_series(now):
    cal = SERIES.replace("DTSTART;TZID=America/New_York:20261003T150000", "DTSTART:20261003T190000Z").replace(
        "DTEND;TZID=America/New_York:20261003T153000", "DTEND:20261003T193000Z"
    )
    inv = meeting_invites.parse(cal)
    assert inv.recurrence["tz"] == "UTC"
    assert inv.start == datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)  # UTC keeps its clock: 2 PM EST now


async def test_a_series_cut_short_releases_its_bot(room, bots, now):
    await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM)
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-1"
    cut = SERIES.replace("BYDAY=SA", "BYDAY=SA;UNTIL=20261101T000000Z").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(cut), TEAM) == "meeting_cancelled"
    bots.leave.assert_awaited_once_with("bot-1")
    assert room.state["meta"]["meeting"]["status"] == "cancelled"


def test_a_lone_occurrence_rsvp_names_the_series_and_the_occurrence():
    inv = meeting_invites.parse(ONE_OCCURRENCE)
    ics = meeting_invites.meeting_rsvp.reply_ics(inv, "agents@acme.thelightbulb.company", datetime(2026, 10, 1))
    assert f"UID:{UID}\r\n" in ics
    assert "RECURRENCE-ID;TZID=America/New_York:20261010T150000\r\n" in ics


def _series_meeting(room, status, **extra):
    _ended_series(room, status=status, **extra)
    return room.state["meta"]["meeting"]


async def test_a_series_reinvite_during_its_call_moves_nothing(room, bots, now):
    meeting = _series_meeting(room, "in_call", inCallAt=1)
    now["t"] = _epoch(2026, 11, 7, 15, 45)  # the call runs past its slot
    resend = SERIES.replace("Weekly sync", "Weekly sync, agenda")
    assert await meeting_invites.handle_team_mail("org_1", _mail(resend), TEAM) == "meeting_scheduled"
    stored = room.state["meta"]["meeting"]
    assert (stored["startAt"], stored["status"], stored["recallBotId"]) == (meeting["startAt"], "in_call", 1)
    bots.reschedule.assert_not_awaited()
    bots.leave.assert_not_awaited()
    bots.create.assert_not_awaited()
    assert not any("moved" in line for line in _lines(room))
    # The call ends: the next occurrence still books.
    room.state["meta"]["meeting"]["status"] = "ended"
    await meeting_invites.advance("r")
    assert room.state["meta"]["meeting"]["startAt"] == _epoch(2026, 11, 14, 15, 0)
    bots.create.assert_awaited_once()


async def test_a_reinvite_after_an_early_end_books_the_next_occurrence_not_this_one(room, bots, now):
    _series_meeting(room, "ended", inCallAt=1)
    now["t"] = _epoch(2026, 11, 7, 15, 10)  # ended 20 minutes early; the invite still says "today"
    assert await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM) == "meeting_scheduled"
    meeting = room.state["meta"]["meeting"]
    assert meeting["startAt"] == _epoch(2026, 11, 14, 15, 0) and meeting["status"] == "bot_created"
    assert bots.create.await_args.kwargs["meeting"]["startAt"] == _epoch(2026, 11, 14, 15, 0)
    assert _lines(room)[-1].endswith("Next: Sat Nov 14, 3:00 PM EST.")


async def test_advance_retries_a_failed_booking_then_succeeds(room, bots, now, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(meeting_invites.asyncio, "sleep", sleep)
    _series_meeting(room, "ended", inCallAt=1, lastBotStatus="done")
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    bots.create.side_effect = [meeting_invites.meeting_bots.RecallError("507"), "bot-2"]
    await meeting_invites.advance("r")
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_created" and meeting["recallBotId"] == "bot-2"
    assert meeting["startAt"] == _epoch(2026, 11, 14, 15, 0)  # the retry kept the date, not skipped a week
    assert (20,) in [c.args for c in sleep.await_args_list]
    assert bots.metric[-1] == ("meeting.advance", {"dimensions": {"outcome": "advanced"}})


async def test_advance_that_never_books_ends_bot_failed_with_a_line(room, bots, now, monkeypatch):
    monkeypatch.setattr(meeting_invites.asyncio, "sleep", AsyncMock())
    _series_meeting(room, "ended", inCallAt=1, lastBotStatus="done")
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    bots.create.side_effect = meeting_invites.MeetingBusy("r")  # a Recall 409 every time
    await meeting_invites.advance("r")
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_failed" and "recallBotId" not in meeting
    assert bots.create.await_count == 3
    assert _bot_lines(room)[-1].startswith("Couldn't book a bot for **Acme weekly sync**")
    assert bots.metric[-1] == ("meeting.advance", {"dimensions": {"outcome": "bot_failed"}})


async def test_a_booking_failure_reforward_keeps_the_nonce_and_the_date(room, bots, now):
    _series_meeting(room, "bot_failed")
    for key in ("recallBotId", "lastBotStatus", "inCallAt", "transcriptAttachedAt", "notesRunId", "notesMailedAt"):
        del room.state["meta"]["meeting"][key]
    room.state["meta"]["meeting"].update(ticketNonce="nonce-1", botClaimAt=1, botError="507")
    now["t"] = _epoch(2026, 11, 7, 12, 0)  # the booked occurrence is still ahead
    assert await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM) == "meeting_scheduled"
    assert bots.create.await_args.kwargs["meeting"]["ticketNonce"] == "nonce-1"
    meeting = room.state["meta"]["meeting"]
    assert meeting["startAt"] == _epoch(2026, 11, 7, 15, 0) and meeting["recallBotId"] == "bot-1"


async def test_advance_seats_an_agent_hired_since_the_room_was_made(room, now, metrics):
    _ended_series(room)
    room.seated.return_value = [{"id": CEO}, {"id": ENG}]
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    room.upsert.assert_awaited_once()
    member = room.upsert.await_args.kwargs["member"]
    assert (member["id"], member["listenMode"], member["role"]) == (HIRED, "mentions", "member")


async def test_a_booking_failure_whose_occurrence_passed_is_a_new_booking(room, bots, now):
    _series_meeting(room, "bot_failed")
    for key in ("recallBotId", "lastBotStatus", "inCallAt", "transcriptAttachedAt", "notesRunId", "notesMailedAt"):
        del room.state["meta"]["meeting"][key]
    room.state["meta"]["meeting"].update(ticketNonce="nonce-1", botClaimAt=1, botError="507")
    now["t"] = _epoch(2026, 11, 8, 12, 0)  # Saturday's slot went by unbooked
    assert await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM) == "meeting_scheduled"
    booked = bots.create.await_args.kwargs["meeting"]
    assert booked["startAt"] == _epoch(2026, 11, 14, 15, 0) and booked["ticketNonce"] != "nonce-1"
    bots.find.assert_not_awaited()  # a nonce claimed just now: no lookup, a fresh create
    meeting = room.state["meta"]["meeting"]
    assert meeting["status"] == "bot_created" and "botError" not in meeting


async def test_a_lost_create_answer_is_adopted_not_booked_twice(room, bots):
    """Recall booked the bot but the answer timed out: the redelivery finds it by its nonce."""
    bots.create.side_effect = meeting_invites.meeting_bots.RecallError("timeout")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    nonce = room.state["meta"]["meeting"]["ticketNonce"]
    bots.find.return_value = "bot-1"
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    bots.create.assert_awaited_once()
    assert bots.find.await_args.args == (room.state["meta"]["roomId"], nonce)
    meeting = room.state["meta"]["meeting"]
    assert meeting["recallBotId"] == "bot-1" and meeting["status"] == "bot_created" and "botError" not in meeting
    bots.audio.assert_awaited_once_with("bot-1")
    assert meeting["audio"] == "separate"  # what the adopted bot really sends


@pytest.mark.parametrize("unknown", [False, True])
async def test_an_adopted_bot_older_than_per_person_audio_is_recorded_as_mixed(room, bots, unknown):
    """A deploy between its create and now: the page must keep publishing the mixed stream, and the
    bridge refuses its socket. A mode Recall cannot tell us reads the same way."""
    bots.create.side_effect = meeting_invites.meeting_bots.RecallError("timeout")
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    bots.find.return_value = "bot-1"
    bots.audio.return_value = None
    bots.audio.side_effect = RuntimeError("recall down") if unknown else None
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert meeting["recallBotId"] == "bot-1" and "audio" not in meeting


async def test_an_abandoned_claim_is_taken_over_once_past_30s(room, bots):
    """The process died between the claim and the bot write: a young claim waits, an old one is
    adopted if Recall has the bot, else booked under the same nonce."""
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    meeting = room.state["meta"]["meeting"]
    nonce = meeting.pop("ticketNonce")
    room.state["meta"]["meeting"] = {
        **{k: v for k, v in meeting.items() if k != "recallBotId"},
        "status": "scheduled",
        "ticketNonce": nonce,
        "botClaimAt": int(time.time()) - 5,
    }
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert bots.create.await_count == 1 and "recallBotId" not in room.state["meta"]["meeting"]
    room.state["meta"]["meeting"]["botClaimAt"] = int(time.time()) - 31
    bots.create.return_value = "bot-2"
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    assert bots.create.await_count == 2 and bots.create.await_args.kwargs["meeting"]["ticketNonce"] == nonce
    assert room.state["meta"]["meeting"]["recallBotId"] == "bot-2"


async def test_a_same_occurrence_booking_retry_adopts_the_bot_its_kept_nonce_booked(room, bots, now):
    _series_meeting(room, "bot_failed")
    for key in ("recallBotId", "lastBotStatus", "inCallAt", "transcriptAttachedAt", "notesRunId", "notesMailedAt"):
        del room.state["meta"]["meeting"][key]
    room.state["meta"]["meeting"].update(ticketNonce="nonce-1", botClaimAt=1, botError="timeout")
    now["t"] = _epoch(2026, 11, 7, 12, 0)
    bots.find.return_value = "bot-9"  # the timed-out create did book it
    await meeting_invites.advance("r")
    bots.find.assert_awaited_once_with("r", "nonce-1")
    bots.create.assert_not_awaited()
    meeting = room.state["meta"]["meeting"]
    assert meeting["recallBotId"] == "bot-9" and meeting["status"] == "bot_created"
    assert meeting["startAt"] == _epoch(2026, 11, 7, 15, 0) and "botError" not in meeting


async def test_every_standing_delivery_records_the_meeting_on_the_team_row(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    moved = GOOGLE.replace("SEQUENCE:0", "SEQUENCE:1").replace("T150000", "T160000")
    await meeting_invites.handle_team_mail("org_1", _mail(moved), TEAM)
    room_id = meeting_invites.messaging_repo.meeting_room_id("org_1", UID)
    assert room.add_meeting.await_args_list == [((TEAM["address"], room_id),)] * 2  # an idempotent ADD
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_stale"
    assert room.add_meeting.await_count == 2  # a stale delivery records nothing


async def test_a_failing_team_row_write_never_fails_the_invite(room):
    room.add_meeting.side_effect = RuntimeError("dynamodb down")
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"


async def test_a_re_dated_occurrence_keeps_its_transcript_in_history(room, now, metrics):
    old = [{"startAt": 1, "transcriptId": "t0", "transcriptRoomId": "d"}] * 30
    _ended_series(room, transcriptId="t1", transcriptRoomId="default-room", history=old)
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    history = room.state["meta"]["meeting"]["history"]
    assert len(history) == 30
    assert history[0] == {
        "startAt": _epoch(2026, 11, 7, 15, 0),
        "transcriptId": "t1",
        "transcriptRoomId": "default-room",
        **{
            k: 1
            for k in (
                "endedAt",
                "notesRunId",
                "notesSkipped",
                "notes",
                "decisions",
                "decisionsRememberedAt",
                "unsaid",
                "followUps",
            )  # fmt: skip
        },  # _ended_series sets each to 1
    }


async def test_history_keeps_each_occurrences_end_notes_and_unsaid_but_unsaid_only_for_the_newest(room, now, metrics):
    """Codex P2: an advance dropped endedAt, the notes state and unsaid with the occurrence keys."""
    unsaid = [{"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "q", "answer": "a", "at": 5}]
    old = [{"startAt": i, "transcriptId": f"t{i}", "unsaid": unsaid} for i in range(5)]
    _ended_series(
        room,
        transcriptId="t9",
        endedAt=99,
        notesRunId="run-1",
        notes="Meeting notes",
        followUps=[{"id": "action-1", "text": "Pat: book venue", "status": "recorded"}],
        decisions=["Ship Friday"],
        notesSkipped=None,
        unsaid=unsaid,
        history=old,
    )
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    history = room.state["meta"]["meeting"]["history"]
    assert history[0]["endedAt"] == 99 and history[0]["notesRunId"] == "run-1"
    assert history[0]["notes"] == "Meeting notes" and history[0]["decisions"] == ["Ship Friday"]
    assert history[0]["followUps"] == [{"id": "action-1", "text": "Pat: book venue", "status": "recorded"}]
    assert "followUps" not in room.state["meta"]["meeting"]
    assert "notes" not in room.state["meta"]["meeting"] and "decisions" not in room.state["meta"]["meeting"]
    assert "notesSkipped" not in history[0]  # only the fields the occurrence has
    assert [bool(h.get("unsaid")) for h in history] == [True, True, True, False, False, False]
    assert "unsaid" not in room.state["meta"]["meeting"]  # the next occurrence starts clean


async def test_an_occurrence_without_a_transcript_adds_no_history(room, now, metrics):
    _ended_series(room)
    room.state["meta"]["meeting"].pop("transcriptId")
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    await meeting_invites.advance("r")
    assert "history" not in room.state["meta"]["meeting"]


# --- fix round 2 ---


async def test_a_reinvite_after_an_early_end_keeps_tomorrows_booking(room, bots, now):
    """advance() booked the next occurrence; a same-SEQUENCE resend still parses to today's slot."""
    _series_meeting(room, "bot_created")
    nxt = {"startAt": _epoch(2026, 11, 14, 15, 0), "endAt": _epoch(2026, 11, 14, 15, 30), "recallBotId": "bot-2"}
    room.state["meta"]["meeting"].update(nxt)
    now["t"] = _epoch(2026, 11, 7, 15, 10)  # today's call ended at 15:08
    assert await meeting_invites.handle_team_mail("org_1", _mail(SERIES), TEAM) == "meeting_scheduled"
    meeting = room.state["meta"]["meeting"]
    assert {k: meeting[k] for k in nxt} == nxt and meeting["status"] == "bot_created"
    bots.create.assert_not_awaited()
    bots.reschedule.assert_not_awaited()
    bots.leave.assert_not_awaited()
    assert not any("moved" in line for line in _lines(room))


async def test_a_finished_series_made_a_single_event_books_it_fresh(room, bots, now):
    _series_meeting(room, "ended", inCallAt=1)
    single = GOOGLE.replace("20261003T", "20261120T").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(single), TEAM) == "meeting_scheduled"
    meeting = room.state["meta"]["meeting"]
    assert "recurrence" not in meeting and meeting["status"] == "bot_created" and meeting["recallBotId"] == "bot-1"
    assert meeting["startAt"] == _epoch(2026, 11, 20, 15, 0) and "inCallAt" not in meeting
    bots.reschedule.assert_not_awaited()
    bots.leave.assert_not_awaited()  # the finished bot is not touched; a new one is booked
    assert bots.create.await_args.kwargs["meeting"]["startAt"] == _epoch(2026, 11, 20, 15, 0)


def test_a_second_freq_cannot_slip_past_the_bound(now):
    assert meeting_invites.parse(SERIES.replace("FREQ=WEEKLY;BYDAY=SA", "FREQ=DAILY;FREQ=MINUTELY")) is None


def test_the_exdate_of_the_occurrence_in_progress_still_counts(now):
    now["t"] = _epoch(2026, 11, 7, 15, 10)
    inv = meeting_invites.parse(
        SERIES.replace("SEQUENCE:0", "EXDATE;TZID=America/New_York:20261107T150000\r\nSEQUENCE:0")
    )
    assert inv.start == datetime(2026, 11, 14, 15, 0, tzinfo=NY)
    assert inv.recurrence["exdates"] == [_epoch(2026, 11, 7, 15, 0)]


async def test_the_inviting_member_is_recorded_for_the_after_meeting_mail(room):
    cal = GOOGLE.replace("mailto:Prasiddha@acme.com", "mailto:host@outside.example")
    await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert meeting["invitedBy"] == "prasiddha@acme.com" and meeting["organizer"] == "host@outside.example"


async def test_a_series_made_a_single_event_keeps_the_last_transcript(room, bots, now):
    _series_meeting(room, "ended", inCallAt=1, transcriptId="t1", transcriptRoomId="default-room")
    started = room.state["meta"]["meeting"]["startAt"]
    single = GOOGLE.replace("20261003T", "20261120T").replace("SEQUENCE:0", "SEQUENCE:1")
    await meeting_invites.handle_team_mail("org_1", _mail(single), TEAM)
    meeting = room.state["meta"]["meeting"]
    assert "transcriptId" not in meeting
    assert meeting["history"] == [
        {
            "startAt": started,
            "transcriptId": "t1",
            "transcriptRoomId": "default-room",
            **{
                k: 1
                for k in (
                    "endedAt",
                    "notesRunId",
                    "notesSkipped",
                    "notes",
                    "decisions",
                    "decisionsRememberedAt",
                    "unsaid",
                    "followUps",
                )  # fmt: skip
            },  # _ended_series sets each to 1
        }
    ]


async def test_a_transcript_written_after_the_callers_read_still_lands_in_history(room, now, metrics):
    _ended_series(room)
    stored = room.state["meta"]["meeting"]
    stale = {k: v for k, v in stored.items() if k not in ("transcriptId", "transcriptRoomId")}
    stored.update(transcriptId="t-late", transcriptRoomId="default-room")  # after the caller read
    now["t"] = _epoch(2026, 11, 7, 15, 40)
    assert await meeting_invites._reschedule_locked("org_1", "r", stale, now=now["t"]) == "advanced"
    meeting = room.state["meta"]["meeting"]
    assert meeting["history"][0]["transcriptId"] == "t-late" and "transcriptId" not in meeting


async def test_a_late_answer_from_an_agent_outside_the_default_room_is_surfaced_there(table, monkeypatch):  # noqa: F811
    repo, messaging = meeting_invites.messaging_repo, meeting_invites.messaging
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": FRIDAY, "name": "Friday"}]))
    human = [_member("human", USER_A, "owner")]
    default, hidden = messaging.default_room_id(OWNER), repo.deterministic_room_id(OWNER, USER_A, "meet")
    rooms = (
        (default, human, {}),
        (
            hidden,
            human + [_member("agent", FRIDAY, listen="mentions", handle="friday")],
            {"meeting": {"uid": "a", "title": "Weekly sync"}, "hidden": True},
        ),
    )
    for room_id, members, extra in rooms:
        await repo.create_room(
            owner_id=OWNER,
            room_id=room_id,
            client_room_id=room_id,
            creator_user_id=USER_A,
            name="r",
            kind="channel",
            members=members,
            req_hash=repo.request_hash(room_id),
            extra=extra,
        )
    request = messaging.AgentMessageRequest.model_validate(
        {
            "eventId": "live-late:run-9",
            "roomId": hidden,
            "agentId": FRIDAY,
            "text": "Three tasks open.",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    )
    first = await messaging.create_agent_message(OWNER, request)
    again = await messaging.create_agent_message(OWNER, request)  # a replay posts nothing new
    assert str(first.room_id) == default and again.message_id == first.message_id
    assert (await repo.get_room(default))["lastSeq"] == 1 and (await repo.get_room(hidden))["lastSeq"] == 0
    line = await repo.get_message_by_id(default, str(first.message_id))
    assert line["author"]["kind"] == "system"
    assert line["text"] == "**Friday**, after **Weekly sync**:\n\nThree tasks open."


# --- stale invites (prod 2026-10-04: two bots sent into a Meet for August occurrences) ---

OLD_OCCURRENCE = (
    GOOGLE.replace("UID:7kukuqrfedlm2f9t0vr42q2gjv@google.com", "UID:old_R20260826T000000@google.com")
    .replace("20261003T150000", "20260826T160000")
    .replace("20261003T153000", "20260826T163000")
    .replace("SEQUENCE:0", "RECURRENCE-ID;TZID=America/New_York:20260826T160000\r\nSEQUENCE:0")
)


@pytest.mark.parametrize(
    "now_ny", [(2026, 9, 30, 12, 0), (2026, 10, 3, 16, 30)], ids=["five-weeks-old", "ended-an-hour-ago"]
)
async def test_an_invite_for_something_already_over_is_ignored(room, bots, monkeypatch, now_ny):
    cal = OLD_OCCURRENCE if now_ny[1] == 9 else GOOGLE
    monkeypatch.setattr(meeting_invites.time, "time", lambda: _epoch(*now_ny))
    room.accept.return_value = True
    assert await meeting_invites.handle_team_mail("org_1", _mail(cal), TEAM) == "meeting_ignored"
    room.create.assert_not_awaited()
    room.post.assert_not_awaited()
    bots.create.assert_not_awaited()
    room.accept.assert_not_awaited()
    assert ("meeting.invite.stale", {}) in bots.metric


async def test_a_meeting_in_progress_still_books_and_joins(room, bots, monkeypatch):
    monkeypatch.setattr(meeting_invites.time, "time", lambda: _epoch(2026, 10, 3, 15, 5))
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    room.create.assert_awaited_once()
    bots.create.assert_awaited_once()


async def test_a_move_into_the_past_leaves_the_record_and_its_bot(room, bots, monkeypatch):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)
    before = dict(room.state["meta"]["meeting"])
    monkeypatch.setattr(meeting_invites.time, "time", lambda: _epoch(2026, 10, 3, 12, 0))
    past = GOOGLE.replace("20261003T15", "20261002T15").replace("SEQUENCE:0", "SEQUENCE:1")
    assert await meeting_invites.handle_team_mail("org_1", _mail(past), TEAM) == "meeting_ignored"
    assert room.state["meta"]["meeting"] == before
    bots.reschedule.assert_not_awaited()
    bots.leave.assert_not_awaited()
    assert bots.create.await_count == 1


async def test_sync_bot_books_nothing_into_a_slot_that_is_over(room, bots):
    room.state["meta"] = {"roomId": "r", "ownerId": "org_1", "revision": 0, "meeting": {}}
    meeting = {
        "uid": UID,
        "sequence": 0,
        "status": "scheduled",
        "startAt": BEFORE_THE_INVITES - 7200,
        "endAt": BEFORE_THE_INVITES - 5400,
        "meetingUrl": "https://meet.google.com/abc-defg-hij",
    }
    room.state["meta"]["meeting"] = dict(meeting)
    assert await meeting_invites._sync_bot("org_1", "r", meeting, None) == meeting
    bots.create.assert_not_awaited()
    assert room.state["meta"]["meeting"] == meeting


async def test_without_a_default_room_lines_go_to_the_organizers_dm_with_the_notes_agent(monkeypatch):
    """10-09: the founder workspace has no default room, so its meeting notes sat in the hidden
    meeting room. They go to the organizer's DM with the agent that writes them."""
    from core.services import openclaw_work

    created = []

    async def create_room(auth, request):
        created.append((auth, request))
        return SimpleNamespace(room_id="dm-1")

    members = [
        {"kind": "human", "id": "user_org", "role": "owner"},
        {"kind": "agent", "id": "ceo-agent"},
        {"kind": "agent", "id": "forge"},
    ]
    monkeypatch.setattr(meeting_invites.messaging_repo, "get_room", AsyncMock(return_value=None))  # no default
    monkeypatch.setattr(meeting_invites.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(openclaw_work, "_fallback_single", AsyncMock(return_value="ceo-agent"))
    monkeypatch.setattr(meeting_invites.messaging, "create_room", create_room)
    assert await meeting_invites.output_room("org_1", "meeting-room") == "dm-1"
    ((auth, request),) = created
    assert (auth.user_id, auth.org_id) == ("user_org", "org_1")
    assert request.kind == "dm" and [(m.kind, m.id) for m in request.members] == [("agent", "ceo-agent")]
    # The same organizer and agent always name the same DM (found, not duplicated, on the next line).
    await meeting_invites.output_room("org_1", "meeting-room")
    assert created[0][1].client_room_id == created[1][1].client_room_id
    # A DM that cannot be opened leaves the line in the meeting's own room: nothing is lost.
    monkeypatch.setattr(
        meeting_invites.messaging,
        "create_room",
        AsyncMock(side_effect=meeting_invites.messaging.ApiProblem("ROOM_FORBIDDEN", "no")),
    )
    assert await meeting_invites.output_room("org_1", "meeting-room") == "meeting-room"


async def test_verified_secondary_sender_uses_current_member_identity(room):
    out = await meeting_invites.handle_team_mail(
        "org_1", _mail(GOOGLE, sender="secondary@acme.com"), TEAM, verified_sender_user_id="user_p"
    )
    assert out == "meeting_scheduled"
    assert room.create.await_args.kwargs["creator_user_id"] == "user_p"


@pytest.mark.parametrize("member", ["user_removed", "user_p"])
async def test_verified_sender_still_requires_member_and_authenticated_from(room, member):
    out = await meeting_invites.handle_team_mail(
        "org_1",
        _mail(GOOGLE, sender="secondary@acme.com", dmarc="FAIL" if member == "user_p" else "PASS"),
        TEAM,
        verified_sender_user_id=member,
    )
    assert out is None
    room.create.assert_not_awaited()


async def test_history_byte_budget_bounds_long_notes_actions_and_unsaid_on_every_write(room):
    """Late notes can land after advance; all history writes must stay below META's 400KiB limit."""
    _ended_series(room, notes="current" * 20000, unsaid=[{"answer": "c" * 44000}])
    history = [
        {
            "startAt": 30 - n,
            "transcriptId": f"t{n}",
            "transcriptRoomId": "general",
            "notesRunId": f"run{n}",
            "notes": "会議" * 3000,
            "followUps": [{"id": f"a{i}", "text": "調整" * 500, "status": "recorded"} for i in range(6)],
            **({"unsaid": [{"answer": "x" * 44000}]} if n < 3 else {}),
        }
        for n in range(30)
    ]
    original = copy.deepcopy(history)
    await meeting_invites._patch_meeting("r", {"history": history})
    kept = room.state["meta"]["meeting"]["history"]
    assert len(json.dumps(kept, ensure_ascii=False).encode()) <= 200 * 1024
    assert 1 <= len(kept) < 30
    assert len(json.dumps(room.state["meta"], ensure_ascii=False, default=str).encode()) < 400 * 1024
    assert room.state["meta"]["meeting"]["notes"] == "current" * 20000
    assert kept[0]["transcriptId"] == "t0" and kept[-1]["startAt"] > 1
    assert history == original  # retention never edits the caller's canonical payload in place
    # A later notes callback grows an archived occurrence after the original retention pass.
    await meeting_invites._patch_meeting(
        "r", lambda m: {"history": [{**h, "notes": "n" * 90000} for h in m["history"]]}
    )
    assert len(json.dumps(room.state["meta"]["meeting"]["history"], ensure_ascii=False).encode()) <= 200 * 1024


async def test_oversized_newest_history_keeps_canonical_refs_with_explicit_omission(room):
    _ended_series(room)
    entry = {
        "startAt": 10,
        "endedAt": 20,
        "transcriptId": "transcript",
        "transcriptRoomId": "general",
        "notesRunId": "canonical-notes-run",
        "notes": "x" * 220000,
        "followUps": [{"id": "a1", "text": "Pat: book venue", "status": "recorded"}],
    }
    original = copy.deepcopy(entry)
    await meeting_invites._patch_meeting("r", {"history": [entry]})
    [kept] = room.state["meta"]["meeting"]["history"]
    assert kept["transcriptId"] == "transcript" and kept["notesRunId"] == "canonical-notes-run"
    assert kept["detailsOmitted"] is True
    assert "notes" not in kept and "followUps" not in kept
    assert len(json.dumps(kept).encode()) <= 200 * 1024
    assert entry == original


async def test_new_meeting_uses_selected_uuid_for_reserved_front_handle(room, monkeypatch):
    # An unrelated worker can have the same edited display name; it cannot take the front-door alias.
    roster = [{"id": CEO, "name": "Lightbulb"}, {"id": ENG, "name": "Lightbulb"}]
    monkeypatch.setattr(meeting_invites.messaging, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(meeting_invites.messaging, "_front_agent_id", AsyncMock(return_value=ENG))
    invite = meeting_invites.parse(GOOGLE)
    assert invite is not None
    await meeting_invites._create_room("org_1", "room-front", invite, "user_p", {"status": "scheduled"})
    seats = {member["id"]: member for member in room.create.await_args.kwargs["members"] if member["kind"] == "agent"}
    assert room.create.await_args.kwargs["extra"]["frontAgentId"] == ENG
    assert seats[ENG]["mentionHandle"] == "lightbulb"
    assert seats[CEO]["mentionHandle"] != "lightbulb"
