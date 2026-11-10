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
    body = {
        "date": day,
        "service": "Gel manicure",
        "name": "Priya",
        "windowFrom": "13:00",
        "windowTo": "16:00",
        "phone": "+19998887777",
    }  # a phone the model supplies is ignored
    r = client.post(_url(call), json=body, headers=W)
    assert r.status_code == 200 and r.json()["waitlisted"] is True
    rows = await voice_phone._booking_rows("org_1", f"VOICEWAIT#{day}#")
    assert len(rows) == 1 and rows[0]["phone"] == "+14155550133" and rows[0]["name"] == "Priya"
    assert rows[0]["windowFrom"] == "13:00" and rows[0]["service"] == "Gel manicure"
    # TTL: a day after the date, i.e. two days after its local midnight
    midnight = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=voice_phone._TZ).timestamp()
    assert int(rows[0]["expiresAt"]) == int(midnight) + 2 * 86400
    assert (await messaging_repo.get_room(call["callId"]))["call"]["steps"][-1]["text"].startswith("Waitlisted")


async def test_a_retry_after_a_failed_call_record_write_adds_no_second_entry(client):  # noqa: F811
    from unittest.mock import AsyncMock, patch

    call = await _start(client)
    day = _tomorrow()
    for i in range(2):  # two other entries, so a duplicate would also hit the cap
        client.post(_url(call), json={"date": day, "service": f"s{i}"}, headers=W)
    body = {"date": day, "service": "Gel", "windowFrom": "13:00"}
    real = voice_phone._patch_call
    with patch.object(voice_phone, "_patch_call", AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            client.post(_url(call), json=body, headers=W)
    assert len(await voice_phone._booking_rows("org_1", f"VOICEWAIT#{day}#")) == 3
    assert voice_phone._patch_call is real
    retry = client.post(_url(call), json=body, headers=W)  # the model retries the same request
    assert retry.status_code == 200 and retry.json()["waitlisted"] is True
    assert len(await voice_phone._booking_rows("org_1", f"VOICEWAIT#{day}#")) == 3


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


async def test_waitlist_is_capped_at_three_per_caller_per_day(client):  # noqa: F811
    call = await _start(client)
    day = _tomorrow()
    for i in range(3):  # different asks; an identical one is a retry, not another entry
        assert client.post(_url(call), json={"date": day, "service": f"s{i}"}, headers=W).status_code == 200
    assert client.post(_url(call), json={"date": day, "service": "s3"}, headers=W).status_code == 429
    other_day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    assert client.post(_url(call), json={"date": other_day}, headers=W).status_code == 200
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
    table.put_item(
        Item={
            "pk": pk,
            "sk": "VOICEWAIT#2026-01-01#old",
            "id": "old",
            "date": "2026-01-01",
            "name": "Expired",
            "expiresAt": 1,
        }
    )  # past its TTL but not yet swept by DynamoDB
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
