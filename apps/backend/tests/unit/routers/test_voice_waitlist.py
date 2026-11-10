"""Waitlist (board 3.24, join half): a caller joins through the front desk, the owner sees and removes entries."""

from datetime import datetime, timedelta

import pytest

from core.repositories import messaging_repo
from routers import voice_phone
from tests.unit.routers.test_voice_phone import W, _start, _tomorrow, client  # noqa: F401
from tests.unit.services.conftest import repos  # noqa: F401


def _url(call) -> str:
    return f"/api/v1/internal/voice/calls/{call['callId']}/waitlist"


async def test_a_caller_joins_the_waitlist_keyed_to_their_number(client):  # noqa: F811
    call = await _start(client)
    day = _tomorrow()
    body = {"date": day, "service": "Gel manicure", "name": "Priya", "note": "after 1pm", "phone": "+19998887777"}
    r = client.post(_url(call), json=body, headers=W)  # a phone the model supplies is ignored
    assert r.status_code == 200 and r.json()["waitlisted"] is True
    rows = await voice_phone._booking_rows("org_1", f"VOICEWAIT#{day}#")
    assert len(rows) == 1 and rows[0]["sk"] == f"VOICEWAIT#{day}#{call['callId']}"
    assert rows[0]["phone"] == "+14155550133" and rows[0]["name"] == "Priya"
    assert rows[0]["note"] == "after 1pm" and rows[0]["service"] == "Gel manicure"
    midnight = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=voice_phone._TZ).timestamp()
    assert int(rows[0]["expiresAt"]) == int(midnight) + 2 * 86400  # TTL: a day after the date
    assert (await messaging_repo.get_room(call["callId"]))["call"]["steps"][-1]["text"].startswith("Waitlisted")


async def test_asking_again_on_the_same_call_is_one_entry(client):  # noqa: F811
    call = await _start(client)
    day = _tomorrow()
    for note in ("after 1pm", "after 2pm"):
        assert client.post(_url(call), json={"date": day, "note": note}, headers=W).status_code == 200
    rows = await voice_phone._booking_rows("org_1", f"VOICEWAIT#{day}#")
    assert len(rows) == 1 and rows[0]["note"] == "after 2pm"


async def test_a_withheld_caller_cannot_join_the_waitlist(client):  # noqa: F811
    call = await _start(client, caller="")
    r = client.post(_url(call), json={"date": _tomorrow()}, headers=W)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "caller_number_required"
    assert await voice_phone._booking_rows("org_1", "VOICEWAIT#") == []


async def test_waitlist_off_refuses_server_side(client):  # noqa: F811
    call = await _start(client, allow={"waitlist": False})
    r = client.post(_url(call), json={"date": _tomorrow()}, headers=W)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert await voice_phone._booking_rows("org_1", "VOICEWAIT#") == []


async def test_waitlist_refuses_a_past_date(client):  # noqa: F811
    call = await _start(client)
    past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    assert client.post(_url(call), json={"date": past}, headers=W).status_code == 400


@pytest.mark.asyncio
async def test_owner_sees_and_removes_waitlist_entries(repos, async_client_org_admin):  # noqa: F811
    from tests.unit.routers import test_voice as tv

    pk = messaging_repo.owner_pk(tv.WS)
    table = messaging_repo._table()
    for sk, name in (("VOICEWAIT#2026-11-20#a", "Priya"), ("VOICEWAIT#2026-11-19#b", "Ana")):
        date, eid = sk.split("#")[1:]
        table.put_item(Item={"pk": pk, "sk": sk, "id": eid, "date": date, "name": name, "phone": "+14155550133"})
    table.put_item(Item={"pk": messaging_repo.owner_pk("org_other"), "sk": "VOICEWAIT#2026-11-19#z", "id": "z"})
    seen = (await async_client_org_admin.get("/api/v1/voice/waitlist")).json()["entries"]
    assert [e["name"] for e in seen] == ["Ana", "Priya"] and seen[0]["phone"] == "+14155550133"
    assert (await async_client_org_admin.delete("/api/v1/voice/waitlist/2026-11-19/b")).status_code == 200
    assert (await async_client_org_admin.delete("/api/v1/voice/waitlist/2026-11-19/b")).status_code == 404
    assert (await async_client_org_admin.delete("/api/v1/voice/waitlist/2026-11-19/z")).status_code == 404  # not theirs
    left = (await async_client_org_admin.get("/api/v1/voice/waitlist")).json()["entries"]
    assert [e["name"] for e in left] == ["Priya"]


@pytest.mark.asyncio
async def test_a_member_reads_but_cannot_remove(repos, async_client_org_member):  # noqa: F811
    assert (await async_client_org_member.get("/api/v1/voice/waitlist")).status_code == 200
    assert (await async_client_org_member.delete("/api/v1/voice/waitlist/2026-11-19/b")).status_code == 403
