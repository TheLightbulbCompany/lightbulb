"""Google phone booking effects, provider uncertainty, and canonical reservations."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException

from core.repositories import messaging_repo
from core.services import voice_booking as booking
from routers import voice_phone as vp
from tests.unit.routers import test_voice_phone
from tests.unit.routers.test_voice_phone import W, _start, _tomorrow

client = test_voice_phone.client
CALENDAR = "calendar-1"


class Google:
    def __init__(self):
        self.events, self.calls, self.failures = {}, [], {}
        self.commit_then_fail = False
        self.busy = []

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path.removeprefix("/calendar/v3")
        failure = self.failures.get((request.method, path))
        if failure:
            return httpx.Response(failure[0], json=failure[1])
        if path == "/freeBusy":
            body = json.loads(request.content)
            return httpx.Response(200, json={"calendars": {x["id"]: {"busy": self.busy} for x in body["items"]}})
        if path in ("/calendars/primary", f"/calendars/{CALENDAR}"):
            return httpx.Response(200, json={"id": CALENDAR})
        prefix = f"/calendars/{CALENDAR}/events"
        if request.method == "GET" and path == prefix:
            q = parse_qs(request.url.query.decode())
            lo, hi = (datetime.fromisoformat(q[k][0]) for k in ("timeMin", "timeMax"))
            events = [
                e for e in self.events.values() if booking._instant(e["start"]) < hi and booking._instant(e["end"]) > lo
            ]
            return httpx.Response(200, json={"items": events})
        if request.method == "POST" and path == prefix:
            body = json.loads(request.content)
            if body["id"] in self.events:
                return httpx.Response(409, json={})
            event = self.events[body["id"]] = {**body, "status": "confirmed", "etag": "v1"}
        else:
            event_id = path.removeprefix(prefix + "/")
            if event_id not in self.events:
                return httpx.Response(404, json={})
            event = self.events[event_id]
            if request.method == "GET":
                return httpx.Response(200, json=event)
            if request.headers.get("If-Match") != event["etag"]:
                return httpx.Response(412, json={})
            if request.method == "DELETE":
                del self.events[event_id]
                return httpx.Response(204)
            assert request.method == "PATCH"
            event = self.events[event_id] = {**event, **json.loads(request.content), "etag": "v2"}
        if self.commit_then_fail:
            self.commit_then_fail = False
            return httpx.Response(500, json={})
        return httpx.Response(200, json=event)


@pytest.fixture
def google(monkeypatch):
    g, real = Google(), httpx.AsyncClient
    monkeypatch.setattr(booking.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(g), **kw))
    monkeypatch.setattr(booking.access_token, "get", AsyncMock(return_value="test-token"))
    return g


async def row(start):
    return await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICEBOOK#{start}")


async def begin(client, **cfg):
    call = await _start(client, bookingsGo="google", **cfg)
    return call, f"/api/v1/internal/voice/calls/{call['callId']}"


def create(client, base, start):
    return client.post(f"{base}/book", json={"start": start, "service": "gel", "name": "Priya"}, headers=W)


def change(client, base, start, target=None):
    return client.post(f"{base}/change", json={"start": start, "newStart": target}, headers=W)


async def test_google_create_move_and_cancel_are_verified_provider_effects(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T14:30"
    assert create(client, base, old).json()["booked"] is True
    original = await row(old)
    assert original["bookingState"] == "confirmed"
    assert original["calendarId"] == CALENDAR and original["eventId"] in google.events
    assert len(original["eventId"]) == 32 and set(original["eventId"]) <= set("0123456789abcdef")
    assert change(client, base, old, new).json()["moved"] is True
    moved = await row(new)
    assert await row(old) is None
    assert moved["bookingId"] == original["bookingId"] and moved["eventId"] == original["eventId"]
    assert booking._instant(google.events[moved["eventId"]]["start"]).minute == 30
    assert change(client, base, new).json()["cancelled"] is True
    assert await row(new) is None and not google.events


@pytest.mark.parametrize("status", [403, 500])
async def test_failed_google_move_does_not_move_or_free_original_booking(client, google, status):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    google.failures[("PATCH", f"/calendars/{CALENDAR}/events/{original['eventId']}")] = (status, {})
    result = change(client, base, old, new).json()
    assert result["moved"] is False
    assert result["state"] == ("pending" if status == 500 else "rejected")
    assert (await row(old))["eventId"] == original["eventId"] and await row(new) is None
    assert create(client, base, old).json().get("booked") is not True or status == 403
    other = await _start(client, caller="+14155550144", room="phone-other", bookingsGo="google")
    other_base = f"/api/v1/internal/voice/calls/{other['callId']}"
    assert create(client, other_base, old).status_code == 409
    if status == 500:
        assert create(client, other_base, new).status_code == 409
    else:
        assert create(client, other_base, new).json()["booked"] is True


@pytest.mark.parametrize("status", [403, 500])
async def test_failed_google_cancel_retains_the_booking_and_occupied_cells(client, google, status):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    original = await row(start)
    google.failures[("DELETE", f"/calendars/{CALENDAR}/events/{original['eventId']}")] = (status, {})
    result = change(client, base, start).json()
    assert result["cancelled"] is False and await row(start)
    for cell in original["cells"]:
        assert await messaging_repo._get(original["pk"], cell)
    assert original["eventId"] in google.events


async def test_unknown_insert_reconciles_same_event_without_another_write(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.commit_then_fail = True
    result = create(client, base, start).json()
    assert result["booked"] is False and result["state"] == "pending"
    original = await row(start)
    assert len(google.events) == 1
    assert create(client, base, start).json()["booked"] is True
    assert (await row(start))["eventId"] == original["eventId"]
    assert len([r for r in google.calls if r.method == "POST" and r.url.path.endswith("/events")]) == 1


async def test_move_reconciles_lost_response_preserving_provider_identity_after_config_change(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    google.commit_then_fail = True
    assert change(client, base, old, new).json()["moved"] is False
    cfg = await messaging_repo.get_voice_agent("org_1")
    await messaging_repo.put_voice_agent("org_1", {**cfg, "bookingsGo": "own"}, "owner")
    assert change(client, base, old, new).json()["moved"] is True
    assert (await row(new))["eventId"] == original["eventId"]
    assert await row(old) is None


async def test_google_move_excludes_only_its_own_event_not_an_overlapping_event(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T14:30"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    google.events["other"] = {**deepcopy(google.events[original["eventId"]]), "id": "other"}
    result = change(client, base, old, new)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "slot_taken"
    assert await row(old) and await row(new) is None
    assert not [r for r in google.calls if r.method == "PATCH"]


@pytest.mark.parametrize("answer", [None, {}, {"busy": [], "errors": [{"reason": "notFound"}]}])
async def test_google_calendar_errors_are_not_verified_availability(client, google, answer):
    _, base = await begin(client)
    google.failures[("POST", "/freeBusy")] = (200, {"calendars": {} if answer is None else {"primary": answer}})
    result = client.post(f"{base}/availability", json={"date": _tomorrow(), "service": "gel"}, headers=W)
    assert result.status_code == 503
    assert result.json()["detail"]["code"] == "provider_availability_unknown"


async def test_failed_preflight_never_inserts_or_claims_a_booking(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.failures[("POST", "/freeBusy")] = (500, {})
    result = create(client, base, start).json()
    assert result["booked"] is False and result["state"] == "unavailable"
    assert await row(start) is None and not google.events


async def test_owner_moving_the_event_in_google_keeps_old_booking(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    event = google.events[original["eventId"]]
    event |= {"etag": "human-edit", "start": {**event["start"], "dateTime": f"{_tomorrow()}T15:00:00"}}
    result = change(client, base, old, new)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "provider_booking_changed"
    assert await row(old) and await row(new) is None


async def test_late_provider_completion_cannot_recreate_or_decorate_a_replacement(client, google, monkeypatch):
    call, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    started, release = asyncio.Event(), asyncio.Event()
    real_request = booking._request

    async def paused(ws, method, url, body=None, **kw):
        if method == "PATCH":
            started.set()
            await release.wait()
        return await real_request(ws, method, url, body, **kw)

    monkeypatch.setattr(booking, "_request", paused)
    cfg = await vp.agent_config("org_1")
    operation = asyncio.create_task(
        booking.move("org_1", cfg, call["callId"], {"caller": "+14155550133"}, original, new)
    )
    await started.wait()
    await run_replace(original)
    release.set()
    with pytest.raises(HTTPException) as exc:
        await operation
    assert exc.value.status_code == 409
    assert (await row(old))["bookingId"] == "replacement" and await row(new) is None
    assert "eventId" not in await row(old)


async def run_replace(original):
    from core.dynamodb import run_in_thread

    replacement = {**original, "bookingId": "replacement", "generation": 1}
    replacement.pop("eventId")
    await run_in_thread(messaging_repo._table().put_item, Item=replacement)


async def test_own_calendar_still_books_moves_and_cancels_without_provider(client, google):
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T14:30"
    assert create(client, base, old).json()["booked"]
    assert change(client, base, old, new).json()["moved"]
    assert change(client, base, new).json()["cancelled"]
    assert not google.calls


async def test_own_booking_stored_before_provider_field_still_moves_and_cancels(client, google):
    from core.dynamodb import run_in_thread

    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T14:30"
    assert create(client, base, old).json()["booked"]
    legacy = await row(old)
    legacy.pop("bookingProvider")
    await run_in_thread(messaging_repo._table().put_item, Item=legacy)
    assert change(client, base, old, new).json()["moved"]
    moved = await row(new)
    moved.pop("bookingProvider", None)
    await run_in_thread(messaging_repo._table().put_item, Item=moved)
    assert change(client, base, new).json()["cancelled"]
    assert not google.calls


async def test_own_booking_stored_before_booking_state_still_counts_as_confirmed(client, google):
    from core.dynamodb import run_in_thread

    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    legacy = await row(start)
    legacy.pop("bookingState")
    legacy.pop("bookingProvider", None)
    await run_in_thread(messaging_repo._table().put_item, Item=legacy)
    assert booking.confirmed(legacy)
    assert await vp._has_upcoming("org_1", legacy["phone"])


async def test_release_tolerates_cells_already_removed_by_ttl(client, google):
    from core.dynamodb import run_in_thread

    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    booked = await row(start)
    assert booked["cells"]
    for cell in booked["cells"]:
        await run_in_thread(messaging_repo._table().delete_item, Key={"pk": booked["pk"], "sk": cell})
    assert change(client, base, start).json()["cancelled"]
    assert await row(start) is None


def test_release_still_refuses_a_cell_another_booking_holds():
    booked = {
        "pk": "OWNER#org_1",
        "sk": "VOICEBOOK#2026-11-11T14:00",
        "bookingId": "b1",
        "generation": 1,
        "createdAt": "c",
        "callId": "k",
        "cells": ["VOICECELL#x"],
    }
    for row_ in (booked, {k: v for k, v in booked.items() if k != "bookingId"}):
        [_, cell] = vp._release("t", "OWNER#org_1", row_)
        condition = cell["Delete"]["ConditionExpression"]
        assert condition.startswith("attribute_not_exists(sk) OR (")
        assert "booking = :booking" in condition


async def test_unknown_cross_day_move_keeps_target_out_of_availability(client, google):
    _, base = await begin(client)
    day = _tomorrow()
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")
    old, new = f"{day}T14:00", f"{next_day}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    google.failures[("PATCH", f"/calendars/{CALENDAR}/events/{original['eventId']}")] = (500, {})
    assert change(client, base, old, new).json()["state"] == "pending"
    result = client.post(f"{base}/availability", json={"date": next_day, "service": "gel"}, headers=W).json()
    assert new not in result["slots"]


async def test_unknown_create_is_reported_as_pending_to_caller_lookup_and_profile(client, google):
    from core.services import business_profile

    call, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.commit_then_fail = True
    assert create(client, base, start).json()["booked"] is False
    found = client.post(f"{base}/my-bookings", headers=W).json()["bookings"]
    assert found[0]["confirmed"] is False and found[0]["state"] == "pending"
    text = await business_profile._callers_bookings("org_1", "+14155550133", datetime.now())
    assert "create not confirmed" in text
    record = await messaging_repo.get_room(call["callId"])
    assert record["call"]["bookings"] == []


async def test_a_crash_before_provider_binding_is_prepared_on_the_same_callers_retry(client, google):
    call, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    cfg = await vp.agent_config("org_1")
    await vp._book("org_1", call["callId"], {"caller": "+14155550133"}, cfg, start, "gel", "Priya")
    held = await row(start)
    assert create(client, base, start).json()["booked"] is True
    assert (await row(start))["bookingId"] == held["bookingId"] and len(google.events) == 1


async def test_a_different_mutation_cannot_bypass_unknown_move(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    google.failures[("PATCH", f"/calendars/{CALENDAR}/events/{original['eventId']}")] = (500, {})
    assert change(client, base, old, new).json()["moved"] is False
    result = change(client, base, old).json()
    assert result["cancelled"] is False and result["code"] == "operation_pending"
    assert not [r for r in google.calls if r.method == "DELETE"]
    assert await row(old)


async def test_changed_configuration_does_not_rebind_confirmed_google_booking(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    cfg = await messaging_repo.get_voice_agent("org_1")
    await messaging_repo.put_voice_agent("org_1", {**cfg, "bookingsGo": "own"}, "owner")
    assert change(client, base, start).json()["cancelled"] is True
    assert not google.events


async def test_cancel_404_reconciliation_requires_access_to_original_calendar(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    original = await row(start)
    event_path = f"/calendars/{CALENDAR}/events/{original['eventId']}"
    google.failures[("DELETE", event_path)] = (500, {})
    assert change(client, base, start).json()["cancelled"] is False
    google.failures[("GET", event_path)] = (404, {})
    google.failures[("GET", f"/calendars/{CALENDAR}")] = (404, {})
    assert change(client, base, start).json()["cancelled"] is False
    assert await row(start)
    del google.failures[("GET", f"/calendars/{CALENDAR}")]
    google.events.clear()
    assert change(client, base, start).json()["cancelled"] is True
    assert await row(start) is None


async def test_concurrent_moves_send_at_most_one_provider_write_and_reserve_only_winner(client, google, monkeypatch):
    call, base = await begin(client)
    old = f"{_tomorrow()}T14:00"
    targets = [f"{_tomorrow()}T16:00", f"{_tomorrow()}T17:00"]
    assert create(client, base, old).json()["booked"]
    original = await row(old)
    count, both = 0, asyncio.Event()
    real = booking._target_free

    async def together(*args):
        nonlocal count
        answer = await real(*args)
        count += 1
        if count == 2:
            both.set()
        await both.wait()
        return answer

    monkeypatch.setattr(booking, "_target_free", together)
    cfg = await vp.agent_config("org_1")
    results = await asyncio.gather(
        *(
            booking.move("org_1", cfg, call["callId"], {"caller": "+14155550133"}, deepcopy(original), target)
            for target in targets
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) and r["state"] == "confirmed" for r in results) == 1
    assert len([r for r in google.calls if r.method == "PATCH"]) == 1
    rows = [await row(target) for target in targets]
    assert sum(r is not None for r in rows) == 1 and await row(old) is None
    assert await vp._booking_rows("org_1", "VOICEHOLD#") == []


async def test_legacy_google_binding_is_explicitly_unverified_without_data_mutation(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    assert create(client, base, start).json()["booked"]
    original = await row(start)
    from core.dynamodb import run_in_thread

    legacy = {
        k: v
        for k, v in original.items()
        if k not in ("calendarId", "bookingId", "bookingProvider", "bookingState", "generation", "providerEvent")
    }
    await run_in_thread(messaging_repo._table().put_item, Item=legacy)
    result = change(client, base, start)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "booking_binding_unverified"
    assert await row(start) == legacy and original["eventId"] in google.events


@pytest.mark.parametrize("problem", ["timeout", "cancelled", "malformed"])
async def test_interrupted_or_malformed_create_remains_unknown_with_its_original_identity(
    client, google, monkeypatch, problem
):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    real = booking._request

    async def interrupted(ws, method, url, body=None, **kw):
        if method == "POST" and url.endswith("/events"):
            if problem == "timeout":
                raise TimeoutError
            if problem == "cancelled":
                raise asyncio.CancelledError
            return {"extendedProperties": {"private": []}}
        return await real(ws, method, url, body, **kw)

    monkeypatch.setattr(booking, "_request", interrupted)
    result = create(client, base, start).json()
    assert result["booked"] is False and result["state"] == "pending"
    held = await row(start)
    assert held["pending"]["state"] == "unknown" and held["eventId"] == held["pending"]["body"]["id"]
    assert create(client, base, start).json()["booked"] is False
    assert (await row(start))["bookingId"] == held["bookingId"]


def posts(google):
    return [r for r in google.calls if r.method == "POST" and r.url.path.endswith("/events")]


async def test_create_that_never_reached_google_is_resent_after_the_in_flight_window(client, google, monkeypatch):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.failures[("POST", f"/calendars/{CALENDAR}/events")] = (500, {})
    assert create(client, base, start).json()["state"] == "pending"
    google.failures.clear()
    # Inside the window the first POST may still be in flight at Google: nothing is resent.
    assert create(client, base, start).json()["state"] == "pending" and len(posts(google)) == 1
    monkeypatch.setattr(booking, "SETTLE_S", 0)
    assert create(client, base, start).json()["booked"] is True
    assert len(posts(google)) == 2 and (await row(start))["eventId"] in google.events


async def test_resent_create_that_google_already_has_is_read_and_settled(client, google, monkeypatch):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.commit_then_fail = True
    assert create(client, base, start).json()["state"] == "pending"
    held = await row(start)
    real = booking._request
    gets = iter([httpx.Response(404, request=httpx.Request("GET", "https://x"))])

    async def first_get_lost(ws, method, url, body=None, **kw):  # the event is there; one read says 404
        if method == "GET" and url.endswith(held["eventId"]) and (r := next(gets, None)):
            raise httpx.HTTPStatusError("lost", request=r.request, response=r)
        return await real(ws, method, url, body, **kw)

    monkeypatch.setattr(booking, "_request", first_get_lost)
    monkeypatch.setattr(booking, "SETTLE_S", 0)
    assert create(client, base, start).json()["booked"] is True  # resent POST answers 409, then GET matches
    assert len(google.events) == 1 and len(posts(google)) == 2


@pytest.mark.parametrize("target", ["T16:00", None])
async def test_move_or_cancel_google_never_applied_releases_after_the_window(client, google, monkeypatch, target):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", target and f"{_tomorrow()}{target}"
    assert create(client, base, old).json()["booked"]
    o = await row(old)
    method = "PATCH" if new else "DELETE"
    google.failures[(method, f"/calendars/{CALENDAR}/events/{o['eventId']}")] = (500, {})
    assert change(client, base, old, new).json()["state"] == "pending"
    google.failures.clear()
    assert change(client, base, old, new).json()["state"] == "pending"  # still inside the in-flight window
    monkeypatch.setattr(booking, "SETTLE_S", 0)
    result = change(client, base, old, new).json()
    assert result["state"] == "rejected"
    kept = await row(old)
    assert "pending" not in kept and kept["eventId"] in google.events
    assert await vp._booking_rows("org_1", "VOICEHOLD#") == []
    if new:
        other = await _start(client, caller="+14155550144", room="phone-other", bookingsGo="google")
        assert create(client, f"/api/v1/internal/voice/calls/{other['callId']}", new).json()["booked"] is True
    again = new and f"{_tomorrow()}T17:00"  # the original stands and still changes
    assert change(client, base, old, again).json()["moved" if new else "cancelled"] is True


async def test_same_caller_reconciles_a_pending_create_from_a_later_call(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.commit_then_fail = True
    assert create(client, base, start).json()["state"] == "pending"
    later = await _start(client, room="phone-later", bookingsGo="google")
    assert create(client, f"/api/v1/internal/voice/calls/{later['callId']}", start).json()["booked"] is True
    assert len(posts(google)) == 1
    other = await _start(client, caller="+14155550144", room="phone-other", bookingsGo="google")
    assert create(client, f"/api/v1/internal/voice/calls/{other['callId']}", start).status_code == 409


async def test_change_on_a_pending_create_finishes_the_create_first(client, google):
    _, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    google.commit_then_fail = True
    assert create(client, base, start).json()["state"] == "pending"
    assert change(client, base, start).json()["cancelled"] is True
    assert await row(start) is None and not google.events and len(posts(google)) == 1


async def test_prepared_operation_is_dispatched_on_resume(client, google, monkeypatch):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    real = booking._dispatch

    async def crashed(ws, cfg, call_id, row_):  # the process died after the prepare save
        return booking._result(row_, "pending", "crashed", action="move")

    monkeypatch.setattr(booking, "_dispatch", crashed)
    assert change(client, base, old, new).json()["moved"] is False
    assert (await row(old))["pending"]["state"] == "prepared"
    monkeypatch.setattr(booking, "_dispatch", real)
    assert change(client, base, old, new).json()["moved"] is True


async def test_google_hold_without_a_prepared_operation_can_be_cancelled(client, google):
    call, base = await begin(client)
    start = f"{_tomorrow()}T14:00"
    cfg = await vp.agent_config("org_1")
    await vp._book("org_1", call["callId"], {"caller": "+14155550133"}, cfg, start, "gel", "Priya")
    cells = (await row(start))["cells"]
    assert change(client, base, start).json()["cancelled"] is True
    assert await row(start) is None and not google.calls
    for cell in cells:
        assert await messaging_repo._get(messaging_repo.owner_pk("org_1"), cell) is None


async def test_line_problem_counts_a_pending_move_as_unconfirmed(client, google, monkeypatch):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    o = await row(old)
    titles, real = [], vp.start_work

    async def spy(call_id, body):
        titles.append(body.title)
        return await real(call_id, body)

    monkeypatch.setattr(vp, "start_work", spy)
    google.failures[("PATCH", f"/calendars/{CALENDAR}/events/{o['eventId']}")] = (500, {})
    change(client, base, old, new)
    assert titles and "1 booking(s)" in titles[0]


async def test_concurrent_failures_raise_one_line_problem(client, google, monkeypatch):
    call, _ = await begin(client)
    titles, real = [], vp.start_work

    async def spy(call_id, body):
        titles.append(body.title)
        await asyncio.sleep(0)
        return await real(call_id, body)

    monkeypatch.setattr(vp, "start_work", spy)
    cfg = await vp.agent_config("org_1")
    await asyncio.gather(*(booking._line_problem("org_1", cfg, call["callId"]) for _ in range(3)))
    assert len(titles) == 1


async def test_owner_edit_in_google_that_keeps_the_time_does_not_block_a_move(client, google):
    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T16:00"
    assert create(client, base, old).json()["booked"]
    o = await row(old)
    google.events[o["eventId"]] |= {"etag": "owner-colour", "colorId": "5"}
    assert change(client, base, old, new).json()["moved"] is True


async def test_lost_move_response_settles_after_shared_cells_aged_out(client, google):
    from core.dynamodb import run_in_thread

    _, base = await begin(client)
    old, new = f"{_tomorrow()}T14:00", f"{_tomorrow()}T14:30"
    assert create(client, base, old).json()["booked"]
    o = await row(old)
    google.commit_then_fail = True
    assert change(client, base, old, new).json()["state"] == "pending"
    for cell in o["cells"]:  # the table's TTL removed the original cells, shared ones included
        await run_in_thread(messaging_repo._table().delete_item, Key={"pk": o["pk"], "sk": cell})
    assert change(client, base, old, new).json()["moved"] is True
    moved = await row(new)
    for cell in moved["cells"]:
        assert (await messaging_repo._get(o["pk"], cell))["booking"] == f"VOICEBOOK#{new}"
