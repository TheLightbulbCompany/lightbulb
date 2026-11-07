"""Where a phone booking goes (`bookingsGo`): Nova's own calendar, or the owner's Google Calendar with Nova's hold
beside it. Google is mocked at the HTTP layer (httpx.MockTransport)."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from core.repositories import messaging_repo
from core.services import voice_booking
from tests.unit.routers import test_voice_phone
from tests.unit.routers.test_voice_phone import W, _start, _tomorrow

client = test_voice_phone.client  # the call fixture: moto table, the worker secret, start_voice_work mocked

API = "https://www.googleapis.com/calendar/v3"


def _z(day: str, hm: str) -> str:  # New York wall time -> what Google sends back (UTC, "Z")
    t = datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=ZoneInfo("America/New_York"))
    return t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Google:
    """Routes "METHOD path" -> response; every request lands in `calls`."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.routes[f"{request.method} {request.url.path.removeprefix('/calendar/v3')}"]

    def sent(self, route: str) -> list[dict]:
        m, path = route.split(" ")
        return [
            json.loads(r.content or b"null") for r in self.calls if (r.method, r.url.path) == (m, f"/calendar/v3{path}")
        ]


@pytest.fixture
def google(monkeypatch):
    g, real = _Google(), httpx.AsyncClient
    monkeypatch.setattr(voice_booking.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(g), **kw))
    return g


@pytest.fixture
def token():
    with patch.object(voice_booking.access_token, "get", AsyncMock(return_value="tok")) as t:
        yield t


async def _row(start: str) -> dict | None:
    return await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICEBOOK#{start}")


async def test_google_free_subtracts_busy(client, token, google):
    day = _tomorrow()
    google.routes["POST /freeBusy"] = httpx.Response(200, json={
        "calendars": {"primary": {"busy": [{"start": _z(day, "14:00"), "end": _z(day, "15:00")}]}}})  # fmt: skip
    google.routes["POST /calendars/primary/events"] = httpx.Response(200, json={"id": "ev0"})
    call = await _start(client, bookingsGo="google")
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    client.post(f"{base}/book", json={"start": f"{day}T11:00", "service": "gel", "name": "Ana"}, headers=W)
    slots = client.post(f"{base}/availability", json={"date": day, "service": "gel"}, headers=W).json()["slots"]
    for busy in ("10:30", "11:00", "11:30", "13:30", "14:00", "14:30"):  # Nova's own cell and Google's busy block
        assert f"{day}T{busy}" not in slots
    assert f"{day}T10:00" in slots and f"{day}T12:00" in slots and f"{day}T15:00" in slots
    [sent] = google.sent("POST /freeBusy")
    assert sent["items"] == [{"id": "primary"}] and sent["timeMin"].startswith(f"{day}T10:00:00")
    assert all(r.headers["Authorization"] == "Bearer tok" for r in google.calls)


async def test_google_book_inserts_and_holds(client, token, google):
    day = _tomorrow()
    google.routes |= {
        "POST /calendars/primary/events": httpx.Response(200, json={"id": "ev1"}),
        "PATCH /calendars/primary/events/ev1": httpx.Response(200, json={"id": "ev1"}),
        "DELETE /calendars/primary/events/ev1": httpx.Response(204),
    }
    call = await _start(client, bookingsGo="google")
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    r = client.post(f"{base}/book", json={"start": f"{day}T14:00", "service": "gel", "name": "Priya"}, headers=W)
    assert r.status_code == 200
    [ev] = google.sent("POST /calendars/primary/events")
    assert ev["start"] == {"dateTime": f"{day}T14:00:00", "timeZone": "America/New_York"}
    assert ev["end"] == {"dateTime": f"{day}T15:00:00", "timeZone": "America/New_York"}
    row = await _row(f"{day}T14:00")
    assert row["eventId"] == "ev1" and row["cells"]  # Nova's hold and its cells, plus the event id

    moved = client.post(f"{base}/change", json={"start": f"{day}T14:00", "newStart": f"{day}T16:00"}, headers=W)
    assert moved.status_code == 200
    assert google.sent("PATCH /calendars/primary/events/ev1")[0]["start"]["dateTime"] == f"{day}T16:00:00"
    assert (await _row(f"{day}T16:00"))["eventId"] == "ev1" and await _row(f"{day}T14:00") is None

    assert client.post(f"{base}/change", json={"start": f"{day}T16:00"}, headers=W).status_code == 200
    assert google.sent("DELETE /calendars/primary/events/ev1") and await _row(f"{day}T16:00") is None


async def test_google_expired_grant_holds_and_raises_line_problem(client, google):
    day = _tomorrow()
    expired = AsyncMock(side_effect=ValueError("google: refresh failed"))
    with (
        patch.object(voice_booking.access_token, "get", expired),
        patch.object(voice_booking.tasks_repo, "get_task", AsyncMock(return_value={"status": "waiting"})),
    ):
        call = await _start(client, bookingsGo="google")
        base = f"/api/v1/internal/voice/calls/{call['callId']}"
        r = client.post(f"{base}/book", json={"start": f"{day}T14:00", "service": "gel", "name": "Priya"}, headers=W)
        assert r.status_code == 200  # the caller's booking is still good
        row = await _row(f"{day}T14:00")
        assert row and "eventId" not in row and row["cells"]
        assert client.start_work.await_count == 1
        assert client.start_work.await_args.args[1].title == (
            "Google Calendar sign-in expired; 1 booking(s) held in Nova's calendar"
        )
        client.post(f"{base}/book", json={"start": f"{day}T17:00", "service": "gel", "name": "Priya"}, headers=W)
        assert client.start_work.await_count == 1  # at most one open per workspace


async def test_own_calendar_unchanged(client, token, google):
    day = _tomorrow()
    call = await _start(client)  # no bookingsGo: the Agent page's default, "own"
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    client.post(f"{base}/availability", json={"date": day, "service": "gel"}, headers=W)
    ok = [
        client.post(f"{base}/book", json={"start": f"{day}T14:00", "service": "gel", "name": "A"}, headers=W),
        client.post(f"{base}/change", json={"start": f"{day}T14:00", "newStart": f"{day}T15:00"}, headers=W),
        client.post(f"{base}/change", json={"start": f"{day}T15:00"}, headers=W),
    ]
    assert [r.status_code for r in ok] == [200, 200, 200]
    assert not google.calls and not token.await_count and client.start_work.await_count == 0
