"""Closures (Agent page `holidays`): a closed date offers no slots and refuses a booking; open dates are unchanged."""

from datetime import datetime

import pytest
from fastapi import HTTPException

from core.voice.phone import calls as phone_calls

CLOSED = [{"from": "2026-11-26", "to": "2026-11-27"}]  # Thu-Fri
LONG_AGO = datetime(2026, 1, 1)


def cfg(holidays):
    c = {**phone_calls.DEFAULTS, "holidays": holidays}
    c["hours"] = {d: {"open": "09:00", "close": "18:00", "closed": False} for d in phone_calls._DAYS}
    return c


def test_closed_dates_offer_no_slots_and_open_dates_are_unchanged():
    assert phone_calls.free_slots(cfg(CLOSED), "2026-11-26", [], LONG_AGO, 30) == []
    assert phone_calls.free_slots(cfg(CLOSED), "2026-11-27", [], LONG_AGO, 30) == []
    assert phone_calls.free_slots(cfg(CLOSED), "2026-11-25", [], LONG_AGO, 30) == phone_calls.free_slots(
        cfg([]), "2026-11-25", [], LONG_AGO, 30
    )
    assert phone_calls.free_slots(cfg(CLOSED), "2026-11-28", [], LONG_AGO, 30)


def test_a_single_day_closure_and_old_free_text_value():
    assert (
        phone_calls.free_slots(cfg([{"from": "2026-11-26", "to": "2026-11-26"}]), "2026-11-26", [], LONG_AGO, 30) == []
    )
    assert phone_calls.free_slots(cfg([{"from": "2026-11-26"}]), "2026-11-26", [], LONG_AGO, 30) == []
    assert phone_calls.free_slots(
        cfg("closed 26-27 Nov"), "2026-11-26", [], LONG_AGO, 30
    )  # old free text closes nothing
    assert phone_calls.free_slots(cfg([{"from": "", "to": "2026-12-01"}]), "2026-11-26", [], LONG_AGO, 30)


def test_midnight_edge_uses_the_local_date():
    # The last slot of the last closed day is gone; the first slot after local midnight on the next day is not.
    c = cfg(CLOSED)
    c["hours"]["Fri"] = {"open": "00:00", "close": "23:30", "closed": False}
    c["hours"]["Sat"] = {"open": "00:00", "close": "23:30", "closed": False}
    assert phone_calls.free_slots(c, "2026-11-27", [], LONG_AGO, 30) == []
    assert phone_calls.free_slots(c, "2026-11-28", [], LONG_AGO, 30)[0] == "2026-11-28T00:00"


@pytest.mark.asyncio
async def test_booking_on_a_closed_date_is_refused():
    with pytest.raises(HTTPException) as e:
        await phone_calls._book("o", "c", {}, {**cfg(CLOSED), "services": []}, "2026-11-26T10:00", "Cut", "Ana")
    assert e.value.status_code == 400
    assert "slot_unavailable" in str(e.value.detail)
