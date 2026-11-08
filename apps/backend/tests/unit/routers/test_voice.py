"""GET /voice/timeline, POST /voice/calls/{id}/done, GET/PUT /voice/agent against the moto messaging table."""

import pytest

from core.repositories import messaging_repo
from tests.unit.services.conftest import repos  # noqa: F401

WS = "org_test_456"


async def _room(room_id: str, kind: str, created: str, owner: str = WS, **extra):
    t = messaging_repo._table()
    t.put_item(
        Item={
            "pk": messaging_repo.owner_pk(owner),
            "sk": messaging_repo.room_pk(room_id),
            "roomId": room_id,
            "kind": kind,
            "createdAt": created,
        }
    )
    t.put_item(
        Item={
            "pk": messaging_repo.room_pk(room_id),
            "sk": "META",
            "roomId": room_id,
            "ownerId": owner,
            "kind": kind,
            "createdAt": created,
            "revision": 1,
            **extra,
        }
    )


@pytest.mark.asyncio
async def test_timeline_lists_calls_and_meetings_newest_first(repos, async_client_org_admin):  # noqa: F811
    await _room(
        "c1",
        "call",
        "2026-11-07T10:00:00+00:00",
        call={"caller": "+14155550133", "outcome": "booked", "startedAt": "2026-11-07T10:00:00+00:00"},
    )
    await _room(
        "m1",
        "channel",
        "2026-11-01T00:00:00+00:00",
        meeting={
            "title": "Team",
            "startAt": 1794070800,
            "ticketNonce": "secret",
            "recallBotId": "b",
            "roster": {"p1": {"name": "Ana", "in": True}},
        },
    )
    await _room("ch", "channel", "2026-11-07T11:00:00+00:00")  # an ordinary channel: not on the timeline
    await _room("x", "call", "2026-11-07T12:00:00+00:00", owner="other")  # another workspace's

    items = (await async_client_org_admin.get("/api/v1/voice/timeline")).json()["items"]

    assert [i["roomId"] for i in items] == ["m1", "c1"]  # by when it happened, not when the room was made
    assert items[1] | {"type": "call", "outcome": "booked"} == items[1]
    assert items[0]["people"] == ["Ana"] and "ticketNonce" not in items[0] and "recallBotId" not in items[0]


@pytest.mark.asyncio
async def test_done_marks_the_call_resolved(repos, async_client_org_admin):  # noqa: F811
    await _room("c1", "call", "2026-11-07T10:00:00+00:00", call={"outcome": "needs_you"})
    assert (await async_client_org_admin.post("/api/v1/voice/calls/c1/done")).status_code == 200
    call = (await messaging_repo.get_room("c1"))["call"]
    assert call["outcome"] == "needs_you" and call["resolvedAt"]
    assert (await async_client_org_admin.post("/api/v1/voice/calls/nope/done")).status_code == 404


@pytest.mark.asyncio
async def test_agent_config_round_trip_admin_only(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json() == {"config": None}
    cfg = {"name": "Nova", "allow": {"book": True}, "hours": [{"day": "Mon", "open": "09:00"}], "price": 4.5}
    assert (await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})).status_code == 200
    assert (await async_client_org_admin.get("/api/v1/voice/agent")).json() == {"config": cfg}
    big = {"x": "a" * 70_000}
    assert (await async_client_org_admin.put("/api/v1/voice/agent", json={"config": big})).status_code == 413


@pytest.mark.asyncio
async def test_agent_config_member_cannot_write(repos, async_client_org_member):  # noqa: F811
    assert (await async_client_org_member.put("/api/v1/voice/agent", json={"config": {}})).status_code == 403


@pytest.mark.asyncio
async def test_a_greeting_style_without_the_disclosure_words_saves(repos, async_client_org_admin, monkeypatch):  # noqa: F811
    # The worker's DisclosureGuard guarantees the name, "AI assistant" and the recording; the field is only style.
    monkeypatch.setattr("core.config.settings.VOICE_LINE_NUMBER", "+14842079294")  # Go live needs a number
    put = lambda cfg: async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})  # noqa: E731
    assert (await put({"live": True, "greeting": "Hi, warm and brief."})).status_code == 200
    assert (await put({"live": True, "greeting": ""})).status_code == 200


@pytest.mark.asyncio
async def test_handoff_number_must_be_e164(repos, async_client_org_admin):  # noqa: F811
    put = lambda n: async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"handoff": {"number": n}}})  # noqa: E731
    for bad in ("555-1234", "+0123456789", "1-900-555-01", "+1555123456789012", 5125550142):
        r = await put(bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "handoff_number_invalid", bad
    for ok in ("+15125550142", "(512) 555-0142", "1 512 555 0142", "+44 20 7946 0958"):
        r = await put(ok)
        assert r.status_code == 200, ok
    assert r.json()["config"]["handoff"]["number"] == "+442079460958"  # stored as the worker dials it
    assert (await put("")).status_code == 200  # no handoff


async def _caller(owner: str):
    await messaging_repo.run_in_thread(
        messaging_repo._table().put_item,
        Item={
            "pk": messaging_repo.owner_pk(owner),
            "sk": "VOICECALLER#+14155550133",
            "name": "Priya",
            "facts": ["Prefers Saturdays"],
        },
    )


@pytest.mark.asyncio
async def test_forget_caller(repos, async_client_org_admin):  # noqa: F811
    await _caller(WS)
    await _caller("org_other")
    url = "/api/v1/voice/callers/+14155550133/memory"
    assert (await async_client_org_admin.delete(url)).json() == {"ok": True}
    row = await messaging_repo._get(messaging_repo.owner_pk(WS), "VOICECALLER#+14155550133")
    assert row["facts"] == [] and "name" not in row
    other = await messaging_repo._get(messaging_repo.owner_pk("org_other"), "VOICECALLER#+14155550133")
    assert other["facts"] == ["Prefers Saturdays"]
    assert (await async_client_org_admin.delete("/api/v1/voice/callers/+14155550999/memory")).status_code == 404


async def test_invalid_timezone_rejected_on_save(repos, async_client_org_admin):  # noqa: F811
    put = lambda tz: async_client_org_admin.put("/api/v1/voice/agent", json={"config": {"timezone": tz}})  # noqa: E731
    r = await put("Mars/Olympus")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "timezone_invalid"
    assert (await put("America/Chicago")).status_code == 200


async def _month_of_calls():
    """This month: two calls (3 min) and a held meeting (5 min); last month's call and another workspace's are out."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/New_York"))
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this, last = first.isoformat(), (first - timedelta(days=3)).isoformat()
    await _room("c1", "call", this, call={"startedAt": this, "durationSec": 120})
    await _room("c2", "call", this, call={"startedAt": this, "durationSec": 60})
    await _room("c0", "call", last, call={"startedAt": last, "durationSec": 6000})
    await _room("cx", "call", this, owner="other", call={"startedAt": this, "durationSec": 6000})
    t = int(first.timestamp())
    await _room("m1", "channel", last, meeting={"title": "Team", "inCallAt": t + 60, "endedAt": t + 360})


@pytest.mark.asyncio
async def test_usage_used_is_this_months_call_and_meeting_minutes(repos, async_client_org_admin):  # noqa: F811
    await _month_of_calls()
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["used"] == 8


@pytest.mark.asyncio
async def test_usage_included_minutes(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["included"] == 1500


@pytest.mark.asyncio
async def test_usage_overage_rate(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["overageRate"] == 0.35


@pytest.mark.asyncio
async def test_usage_run_out_comes_from_the_agent_page(repos, async_client_org_admin):  # noqa: F811
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["runOut"] == "keep"
    await messaging_repo.put_voice_agent(WS, {"runOut": "forward"}, "u")
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["runOut"] == "forward"


@pytest.mark.asyncio
async def test_usage_reads_the_months_counter_row_when_there_is_one(repos, async_client_org_admin):  # noqa: F811
    from datetime import UTC, datetime

    from core.billing import voice_billing

    await _month_of_calls()  # the scan would say 8
    await voice_billing.add_month_seconds(WS, 600, datetime.now(UTC))
    assert (await async_client_org_admin.get("/api/v1/voice/usage")).json()["used"] == 10


@pytest.mark.asyncio
async def test_handoff_number_cannot_be_the_forwarded_line(repos, async_client_org_admin):  # noqa: F811
    cfg = {"forwardFrom": "(415) 555-0177", "handoff": {"number": "+14155550177"}}
    r = await async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "handoff_is_forwarded_line"
