"""A recurring occurrence has one scheduling owner, even when the master advanced."""

from copy import deepcopy

import pytest

from core.services import meeting_invites as mi
from tests.unit.services import test_meeting_invites as fixtures

TEAM, UID, SERIES = fixtures.TEAM, fixtures.UID, fixtures.SERIES
_mail, _epoch = fixtures._mail, fixtures._epoch
_clock, room, bots, now = fixtures._clock, fixtures.room, fixtures.bots, fixtures.now


@pytest.fixture
def store(room, bots):
    rows = {}

    def create(**kw):
        rows.setdefault(kw["room_id"], {"roomId": kw["room_id"], "ownerId": kw["owner_id"], **deepcopy(kw["extra"])})

    def get(r):
        return deepcopy(rows.get(r))

    def patch(r, updates, expected_revision=None):
        assert expected_revision == rows[r].get("revision", 0)
        rows[r] = {**rows[r], **deepcopy(updates), "revision": expected_revision + 1}

    def absent(r, field, value, also=None):
        m = rows[r]["meeting"]
        if field in m:
            return False
        m.update({field: value, **(also or {})})
        return True

    room.create.side_effect = create
    room.get.side_effect = get
    room.patch.side_effect = patch
    mi.messaging_repo.set_meeting_field_if_absent.side_effect = absent
    bots.create.side_effect = lambda **kw: "bot-" + kw["room_id"]
    return rows


def override(*, hour=16, sequence=1, cancel=False, day="20261107"):
    cal = SERIES.replace(
        "DTSTART;TZID=America/New_York:20261003T150000", f"DTSTART;TZID=America/New_York:{day}T{hour:02}0000"
    ).replace("DTEND;TZID=America/New_York:20261003T153000", f"DTEND;TZID=America/New_York:{day}T{hour:02}3000")
    cal = cal.replace("RRULE:FREQ=WEEKLY;BYDAY=SA", "RECURRENCE-ID;TZID=America/New_York:20261107T150000").replace(
        "SEQUENCE:0", f"SEQUENCE:{sequence}"
    )
    return cal.replace("METHOD:REQUEST", "METHOD:CANCEL") if cancel else cal


PARENT = mi.messaging_repo.meeting_room_id("org_1", UID)
CHILD = mi.messaging_repo.meeting_room_id("org_1", UID + "#20261107T150000")


@pytest.mark.parametrize("hour", [14, 16])
async def test_override_owns_slot_and_parent_moves_to_next_week(store, bots, now, hour):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail("org_1", _mail(override(hour=hour)), TEAM)
    assert store[CHILD]["meeting"]["startAt"] == _epoch(2026, 11, 7, hour, 0)
    assert store[PARENT]["meeting"]["startAt"] == _epoch(2026, 11, 14, 15, 0)
    assert store[CHILD]["meeting"]["recallBotId"] != store[PARENT]["meeting"]["recallBotId"]
    bots.leave.assert_awaited_once()
    count = bots.create.await_count
    await mi.handle_team_mail("org_1", _mail(override(hour=hour)), TEAM)
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    assert store[PARENT]["meeting"]["startAt"] == _epoch(2026, 11, 14, 15, 0)
    assert bots.create.await_count == count


async def test_already_advanced_parent_bot_and_time_are_preserved(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    store[PARENT]["meeting"].update(startAt=_epoch(2026, 11, 14, 15, 0), endAt=_epoch(2026, 11, 14, 15, 30))
    before = deepcopy(store[PARENT]["meeting"])
    await mi.handle_team_mail("org_1", _mail(override()), TEAM)
    assert store[PARENT]["meeting"]["startAt"] == before["startAt"]
    assert store[PARENT]["meeting"]["recallBotId"] == before["recallBotId"]
    bots.leave.assert_not_awaited()
    assert CHILD in store


async def test_future_override_is_skipped_when_parent_advances(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    cal = override().replace("20261107", "20261114")
    await mi.handle_team_mail("org_1", _mail(cal), TEAM)
    store[PARENT]["meeting"]["status"] = "ended"
    now["t"] = _epoch(2026, 11, 7, 16, 0)
    await mi.advance(PARENT)
    assert store[PARENT]["meeting"]["startAt"] == _epoch(2026, 11, 21, 15, 0)


async def test_cancel_before_request_has_tombstone_and_does_not_rebook(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail("org_1", _mail(override(cancel=True, sequence=2)), TEAM)
    assert store[CHILD]["meeting"]["status"] == "cancelled"
    count = bots.create.await_count
    await mi.handle_team_mail("org_1", _mail(override(sequence=1)), TEAM)
    await mi.handle_team_mail("org_1", _mail(override(sequence=2)), TEAM)
    assert store[CHILD]["meeting"]["status"] == "cancelled" and bots.create.await_count == count
    await mi.handle_team_mail("org_1", _mail(override(sequence=3)), TEAM)
    assert store[CHILD]["meeting"]["status"] == "bot_created"


async def test_parent_bot_failure_prevents_child_duplicate(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    bots.leave.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(mi.MeetingBusy):
        await mi.handle_team_mail("org_1", _mail(override()), TEAM)
    assert CHILD not in store
    assert bots.create.await_count == 1
    bots.leave.side_effect = None
    await mi.handle_team_mail("org_1", _mail(override()), TEAM)
    assert store[CHILD]["meeting"]["status"] == "bot_created"


async def test_existing_child_move_then_cancel_only_changes_child(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail("org_1", _mail(override()), TEAM)
    parent = deepcopy(store[PARENT]["meeting"])
    await mi.handle_team_mail("org_1", _mail(override(hour=17, sequence=2)), TEAM)
    assert store[CHILD]["meeting"]["startAt"] == _epoch(2026, 11, 7, 17, 0)
    bots.reschedule.assert_awaited_once()
    cancel = override(sequence=3, cancel=True).replace("DTSTART;TZID=America/New_York:20261107T160000\r\n", "")
    await mi.handle_team_mail("org_1", _mail(cancel), TEAM)
    assert store[CHILD]["meeting"]["status"] == "cancelled"
    assert "recallBotId" not in store[CHILD]["meeting"]
    assert store[PARENT]["meeting"] == parent


async def test_invalid_instance_date_is_mail_not_a_retry_loop(store, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    bad = override().replace(
        "RECURRENCE-ID;TZID=America/New_York:20261107T150000", "RECURRENCE-ID;TZID=America/New_York:invalid"
    )
    assert await mi.handle_team_mail("org_1", _mail(bad), TEAM) is None


async def test_authenticated_different_organizer_cannot_detach_instance(store, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    before = deepcopy(store)
    forged = override().replace("mailto:Prasiddha@acme.com", "mailto:dana@acme.com")
    assert await mi.handle_team_mail("org_1", _mail(forged, sender="dana@acme.com"), TEAM) == "meeting_ignored"
    assert store == before


async def test_cancelled_master_cannot_spawn_late_child(store, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail(
        "org_1", _mail(SERIES.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:2")), TEAM
    )
    assert await mi.handle_team_mail("org_1", _mail(override()), TEAM) == "meeting_ignored"
    assert CHILD not in store


async def test_whole_series_cancel_pulls_future_detached_children(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail("org_1", _mail(override(sequence=4)), TEAM)
    await mi.handle_team_mail(
        "org_1", _mail(SERIES.replace("METHOD:REQUEST", "METHOD:CANCEL").replace("SEQUENCE:0", "SEQUENCE:2")), TEAM
    )
    assert store[PARENT]["meeting"]["status"] == "cancelled"
    assert store[CHILD]["meeting"]["status"] == "cancelled"
    assert "recallBotId" not in store[CHILD]["meeting"]


async def test_failed_child_cancel_retries_without_losing_bot_identity(store, bots, now):
    await mi.handle_team_mail("org_1", _mail(SERIES), TEAM)
    await mi.handle_team_mail("org_1", _mail(override()), TEAM)
    child_bot = store[CHILD]["meeting"]["recallBotId"]
    bots.leave.side_effect = RuntimeError("retryable provider failure")
    with pytest.raises(mi.MeetingBusy):
        await mi.handle_team_mail("org_1", _mail(override(cancel=True, sequence=2)), TEAM)
    assert store[CHILD]["meeting"]["recallBotId"] == child_bot
    bots.leave.side_effect = None
    await mi.handle_team_mail("org_1", _mail(override(cancel=True, sequence=2)), TEAM)
    assert "recallBotId" not in store[CHILD]["meeting"]
