"""Mocked Acuity wire contracts (shapes from developers.acuityscheduling.com); no real Acuity calls."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import voice_booking_acuity as ac

WS = "org_salon"
BINDING = ac.WorkspaceBinding(WS, "123")
START = "2026-12-01T10:00:00-0500"
APPT = {"id": 99, "datetime": START, "appointmentTypeID": 7, "calendarID": 3, "phone": "(484) 555-0123",
        "firstName": "Ann", "lastName": "Lee", "canceled": False}  # fmt: skip
BOOK = dict(appointment_type_id=7, start=START, first_name="Ann", last_name="Lee", email="a@x.com",
            phone="+14845550123", calendar_id=3)  # fmt: skip


@pytest.fixture
def wire(monkeypatch):
    original = httpx.AsyncClient
    state = {"requests": [], "credential": {"token": "123:secret"}, "routes": {}}

    @asynccontextmanager
    async def guard(ws):
        yield

    async def handle(request):
        state["requests"].append(request)
        route = state["routes"].get((request.method, request.url.path))
        if isinstance(route, Exception):
            raise route
        return route or httpx.Response(200, json=[])

    monkeypatch.setattr(ac.owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(ac.integration_secret_store, "get", AsyncMock(side_effect=lambda ws, p: state["credential"]))
    monkeypatch.setattr(ac.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return state


def writes(wire):
    return [r for r in wire["requests"] if r.method != "GET"]


async def test_reads_use_basic_auth_and_map_types_staff_times(wire):
    wire["routes"] = {
        ("GET", "/api/v1/appointment-types"): httpx.Response(
            200, json=[{"id": 7, "name": "Cut", "duration": 45, "active": True},
                       {"id": 8, "name": "Hidden", "duration": 30, "active": True, "private": True}]),
        ("GET", "/api/v1/calendars"): httpx.Response(200, json=[{"id": 3, "name": "Sam"}]),
        ("GET", "/api/v1/availability/times"): httpx.Response(200, json=[{"time": START, "slotsAvailable": 1}]),
        ("GET", "/api/v1/availability/dates"): httpx.Response(200, json=[{"date": "2026-12-01"}]),
    }  # fmt: skip
    assert [t["id"] for t in await ac.appointment_types(WS, BINDING)] == [7]
    assert (await ac.calendars(WS, BINDING))[0]["name"] == "Sam"
    assert await ac.times(WS, BINDING, date="2026-12-01", appointment_type_id=7, calendar_id=3) == [START]
    assert await ac.dates(WS, BINDING, month="2026-12", appointment_type_id=7) == ["2026-12-01"]
    assert wire["requests"][0].headers["Authorization"].startswith("Basic ")
    assert dict(wire["requests"][2].url.params) == {"date": "2026-12-01", "appointmentTypeID": "7", "calendarID": "3"}
    assert not writes(wire)


async def test_create_books_as_client_never_admin(wire):
    wire["routes"][("POST", "/api/v1/appointments")] = httpx.Response(200, json=APPT)
    assert (await ac.create(WS, BINDING, **BOOK))["id"] == 99
    (post,) = writes(wire)
    assert "admin" not in post.url.params
    assert json.loads(post.content) == {"appointmentTypeID": 7, "datetime": START, "firstName": "Ann",
                                        "lastName": "Lee", "email": "a@x.com", "phone": "+14845550123",
                                        "calendarID": 3}  # fmt: skip


async def test_create_needs_email_and_name_before_any_call(wire):
    for bad in ({"email": ""}, {"last_name": " "}):
        with pytest.raises(ac.AcuityError):
            await ac.create(WS, BINDING, **{**BOOK, **bad})
    assert not wire["requests"]


async def test_taken_slot_is_rejected_not_unknown(wire):
    wire["routes"][("POST", "/api/v1/appointments")] = httpx.Response(
        400, json={"status_code": 400, "error": "not_available", "message": "x"}
    )
    with pytest.raises(ac.AcuityError) as e:
        await ac.create(WS, BINDING, **BOOK)
    assert (e.value.outcome, e.value.provider_code) == ("rejected", "not_available")


@pytest.mark.parametrize("route", [httpx.Response(503, json={}), httpx.ReadTimeout("t"),
                                   httpx.Response(200, json={"id": 1})])  # fmt: skip
async def test_lost_or_garbled_create_is_unknown_and_never_repeated(wire, route):
    wire["routes"][("POST", "/api/v1/appointments")] = route
    with pytest.raises(ac.AcuityError) as e:
        await ac.create(WS, BINDING, **BOOK)
    assert e.value.outcome == "unknown"
    assert len(writes(wire)) == 1


async def test_find_reconciles_a_lost_create_by_type_start_and_caller(wire):
    other = {**APPT, "id": 5, "datetime": "2026-12-01T11:00:00-0500"}
    wire["routes"][("GET", "/api/v1/appointments")] = httpx.Response(200, json=[other, APPT])
    found = await ac.find(WS, BINDING, appointment_type_id=7, start=START, first_name="Ann", last_name="Lee",
                          phone="484-555-0123")  # fmt: skip
    assert found["id"] == 99
    wire["routes"][("GET", "/api/v1/appointments")] = httpx.Response(200, json=[other])
    assert await ac.find(WS, BINDING, appointment_type_id=7, start=START, first_name="Ann", last_name="Lee") is None
    assert not writes(wire)


async def test_reschedule_and_cancel(wire):
    moved = "2026-12-02T10:00:00-0500"
    wire["routes"] = {
        ("GET", "/api/v1/appointments/99"): httpx.Response(200, json=APPT),
        ("PUT", "/api/v1/appointments/99/reschedule"): httpx.Response(200, json={**APPT, "datetime": moved}),
        ("PUT", "/api/v1/appointments/99/cancel"): httpx.Response(200, json={**APPT, "canceled": True}),
    }
    assert (await ac.reschedule(WS, BINDING, 99, start=moved))["datetime"] == moved
    assert json.loads(writes(wire)[0].content) == {"datetime": moved}
    assert (await ac.cancel(WS, BINDING, 99))["canceled"] is True


async def test_cancel_response_not_canceled_is_unknown(wire):
    wire["routes"] = {
        ("GET", "/api/v1/appointments/99"): httpx.Response(200, json=APPT),
        ("PUT", "/api/v1/appointments/99/cancel"): httpx.Response(200, json=APPT),
    }
    with pytest.raises(ac.AcuityError) as e:
        await ac.cancel(WS, BINDING, 99)
    assert e.value.outcome == "unknown"


async def test_account_and_workspace_must_match(wire):
    wire["credential"] = {"token": "999:secret"}
    with pytest.raises(ac.AcuityError, match="account_mismatch"):
        await ac.calendars(WS, BINDING)
    with pytest.raises(ac.AcuityError, match="workspace_mismatch"):
        await ac.calendars("other", BINDING)
    assert not wire["requests"]
