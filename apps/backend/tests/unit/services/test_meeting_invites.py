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
        ("SEQUENCE:0", "RECURRENCE-ID;TZID=America/New_York:20261010T150000\r\nSEQUENCE:0"),
    ],
    ids=["bad-sequence", "zone-directory", "uid-too-long", "recurrence-id"],
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
WITH_DANA = GOOGLE.replace("SEQUENCE:0", "ATTENDEE;CN=Dana:mailto:dana@acme.com\r\nSEQUENCE:0")


@pytest.fixture
def room(monkeypatch):
    """A fake room store: create_room writes META with ``extra``, patch_room_meta merges into it."""
    state = {"meta": None, "lines": {}}

    def post(**kw):  # the repo's receipt: same key + same hash replays, a different hash conflicts
        seen = state["lines"].get(kw["idemp_sk"])
        if seen is not None:
            if seen[0] != kw["req_hash"]:
                raise meeting_invites.messaging_repo.IdempotencyConflict("different request")
            return
        state["lines"][kw["idemp_sk"]] = (kw["req_hash"], kw["text"])

    def create(**kw):
        state["meta"] = {"roomId": kw["room_id"], **kw["extra"]}

    def patch(room_id, updates):
        state["meta"] = {**state["meta"], **updates}

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
    monkeypatch.setattr(repo, "create_message", m.post)
    monkeypatch.setattr(repo, "ensure_user_stream", AsyncMock())
    monkeypatch.setattr(meeting_invites.clerk_admin, "list_organization_members", m.members)
    m.claim = AsyncMock(return_value="tok")
    m.release = AsyncMock()
    monkeypatch.setattr(meeting_invites.container_repo, "try_claim_owner_lifecycle", m.claim)
    monkeypatch.setattr(meeting_invites.container_repo, "release_owner_lifecycle_claim", m.release)
    monkeypatch.setattr(meeting_invites.meeting_rsvp, "accept", m.accept)
    company = SimpleNamespace(starter_agent_ids={"ceo": CEO, "engineering": ENG})
    monkeypatch.setattr(
        meeting_invites, "PaperclipRepo", lambda **_: SimpleNamespace(resolve_owner_row=AsyncMock(return_value=company))
    )
    roster = [
        {"id": CEO, "name": "CEO"},
        {"id": ENG, "name": "Engineering"},
        {"id": "33333333-3333-4333-8333-333333333333", "name": "Hired"},
    ]
    monkeypatch.setattr(meeting_invites.messaging, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(meeting_invites.messaging, "human_display_name", AsyncMock(side_effect=lambda u: u))
    return m


def _lines(room):
    return [text for _, text in room.state["lines"].values()]


async def test_a_members_invite_opens_the_meeting_room(room):
    assert await meeting_invites.handle_team_mail("org_1", _mail(WITH_DANA), TEAM) == "meeting_scheduled"
    room.create.assert_awaited_once()
    kw = room.create.await_args.kwargs
    assert kw["room_id"] == meeting_invites.messaging_repo.meeting_room_id("org_1", UID)
    assert kw["kind"] == "channel" and kw["name"] == "Acme weekly sync — Oct 3" and kw["creator_user_id"] == "user_p"
    assert {(m["id"], m["role"], m["listenMode"]) for m in kw["members"]} == {
        ("user_p", "owner", None),
        ("user_d", "member", None),
        (CEO, "member", "mentions"),
        (ENG, "member", "mentions"),
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
    assert _lines(room)[-1] == "This meeting was cancelled."


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
    room.patch.side_effect = [_cas_lost(), _cas_lost()]
    again = moved.replace("SEQUENCE:1", "SEQUENCE:2")
    assert await meeting_invites.handle_team_mail("org_1", _mail(again), TEAM) == "meeting_scheduled"
    assert room.patch.await_count == 4


async def test_a_lost_race_to_a_newer_sequence_is_not_retried(room):
    await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM)

    def newer_won(room_id, updates):
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
    assert await meeting_invites._patch_meeting(room.state["meta"]["roomId"], {"sequence": 2}) is False
    assert room.state["meta"]["meeting"]["sequence"] == 3


async def test_a_racing_second_creator_falls_through_to_the_update(room):
    def lost_race(**kw):
        room.state["meta"] = {"roomId": kw["room_id"], **kw["extra"]}  # the other member's create landed
        raise meeting_invites.messaging_repo.MessagingRepoError("room create cancelled without a durable receipt")

    room.create.side_effect = lost_race
    assert await meeting_invites.handle_team_mail("org_1", _mail(GOOGLE), TEAM) == "meeting_scheduled"
    assert _lines(room) == ["Agents will join **Acme weekly sync** on Sat Oct 3, 3:00 PM EDT."]


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
    assert _lines(room) == [join, "This meeting was cancelled.", join]
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
