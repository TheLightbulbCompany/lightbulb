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
async def test_a_live_greeting_must_disclose_ai_and_recording(repos, async_client_org_admin):  # noqa: F811
    put = lambda cfg: async_client_org_admin.put("/api/v1/voice/agent", json={"config": cfg})  # noqa: E731
    r = await put({"live": True, "greeting": "Hi, this is Nova. How can I help?"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "greeting_needs_disclosure"
    assert (
        await put({"live": True, "greeting": "This is Nova, the AI assistant. This call is recorded."})
    ).status_code == 200
    assert (await put({"live": True, "greeting": ""})).status_code == 200  # the page's default says both
    assert (await put({"live": False, "greeting": "Hi"})).status_code == 200  # a draft before Go live


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
