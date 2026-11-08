"""The phone receptionist's backend: the call record the owner's Home reads, the Agent page's settings gating the tools,
the assistant's calendar, messages, hand-off and start_work."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from core.repositories import messaging_repo
from routers import voice_phone

SECRET = "s3cret"
W = {"X-Lightbulb-Huddle-Secret": SECRET}
NUMBER = "+14155550100"
LINE = {"ownerId": "org_1", "ownerUserId": "user_owner"}  # number -> workspace only
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CONFIG = {  # the Agent page document
    "name": "Nova",
    "live": True,
    "hours": {d: {"open": "10:00", "close": "19:00"} for d in DAYS},
    "services": [{"name": "Gel manicure", "minutes": 60, "price": "$45", "staff": ""}],
    "bufferMin": 0,
    "knowledge": [{"q": "Parking?", "a": "Behind the building."}],
    "handoff": {"number": "+14155550199", "onlyOpenHours": False},
}


@pytest.fixture
def client():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging"),
            patch.object(messaging_repo.settings, "HUDDLE_WORKER_SECRET", SECRET),
            patch.object(
                voice_phone, "start_voice_work", AsyncMock(return_value={"triggerId": "t", "taskId": "k"})
            ) as sw,
            patch.object(
                voice_phone.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Maya's Nail Studio"})
            ),
        ):
            from main import app

            c = TestClient(app)
            c.start_work = sw
            yield c


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


async def _start(client, caller="+14155550133", room="phone-dev-_x_1", **config):
    await voice_phone.put_line(NUMBER, LINE)
    await messaging_repo.put_voice_agent("org_1", {**CONFIG, **config}, "user_owner")
    r = client.post(
        "/api/v1/internal/voice/calls",
        json={"calledNumber": NUMBER, "callerNumber": caller, "livekitRoom": room},
        headers=W,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_call_books_moves_and_leaves_the_record_home_reads(client):
    call = await _start(client)
    assert call["greeting"] == (
        "Thanks for calling Maya's Nail Studio. This is Nova, Maya's Nail Studio's AI assistant. "
        "This call is recorded. How can I help?"
    )
    assert "Gel manicure $45 (60 min)" in call["facts"] and "Behind the building." in call["facts"]
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    day = _tomorrow()
    slots = client.post(f"{base}/availability", json={"date": day, "service": "gel"}, headers=W).json()["slots"]
    assert slots[0] == f"{day}T10:00" and slots[-1] == f"{day}T18:00" and len(slots) == 17  # half-hourly

    body = {"start": f"{day}T14:00", "service": "gel manicure", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 409  # taken
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T14:30"}, headers=W).status_code == 409  # overlaps
    assert (
        client.post(f"{base}/book", json=body | {"start": f"{day}T18:30"}, headers=W).status_code == 400
    )  # past close
    moved = client.post(f"{base}/change", json={"start": f"{day}T14:00", "newStart": f"{day}T14:30"}, headers=W)
    assert moved.status_code == 200 and moved.json()["moved"]  # its own old slot doesn't block the move
    assert client.post(f"{base}/my-bookings", headers=W).json()["bookings"] == [
        {"start": f"{day}T14:30", "service": "gel manicure"}
    ]
    tx = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello!"}, {"role": "system", "text": "x"}]
    end = client.post(f"{base}/end", json={"durationSec": 160, "summary": "Moved Priya.", "transcript": tx}, headers=W)
    rec = end.json()
    meta = await messaging_repo.get_room(call["callId"])
    assert meta["kind"] == "call" and meta["hidden"] and meta["call"] == rec
    assert rec["outcome"] == "moved" and rec["durationSec"] == 160 and rec["caller"] == "+14155550133"
    assert (
        rec["callerName"] == "Priya" and rec["returning"] is False and rec["booking"].startswith("gel manicure moved")
    )
    assert rec["transcript"] == [{"who": "caller", "text": "hi"}, {"who": "assistant", "text": "Hello!"}]
    assert [s["text"][:6] for s in rec["steps"]] == ["Booked", "Gel ma"] and all(
        isinstance(s["t"], int) for s in rec["steps"]
    )
    assert rec["ownerMode"] is False and rec["resolvedAt"] is None and rec["callerMemory"] == []

    again = await _start(client, room="phone-dev-_x_2")  # the same number calls back
    assert again["callerName"] == "Priya"
    assert (await messaging_repo.get_room(again["callId"]))["call"]["returning"] is True


async def test_a_custom_greeting_missing_the_disclosures_falls_back_to_ours(client):
    ok = "Hi, Nova here, the AI assistant at Maya's. This call is recorded. What can I do?"
    assert (await _start(client, greeting=ok))["greeting"] == ok
    for bad in ("Hi, Nova here at Maya's. How can I help?", "Hi, the AI assistant here. This call is recorded."):
        got = (await _start(client, room=f"phone-dev-_g{len(bad)}", greeting=bad))["greeting"]
        assert got.startswith("Thanks for calling Maya's Nail Studio. This is Nova") and "recorded" in got


async def test_the_record_keeps_each_lines_time_and_the_disclosure_verdict(client):
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    tx = [
        {"who": "assistant", "text": "Thanks for calling.", "t": 1.1, "assumed": True},
        {"who": "caller", "text": "Hey, how are you?", "t": 7.4},
        {"who": "assistant", "text": "Well, thanks!", "t": 9.25},
        {"who": "caller", "text": "no time", "t": "soon"},
    ]
    bad = client.post(url, json={"durationSec": 12, "transcript": tx, "disclosure": "probably"}, headers=W)
    assert bad.status_code == 400  # the app answers request validation errors with 400
    body = {"durationSec": 12, "transcript": tx, "disclosure": "missing", "disclosureRepeated": True}
    end = client.post(url, json=body, headers=W).json()
    assert end["transcript"] == [
        {"who": "assistant", "text": "Thanks for calling.", "t": 1.1},  # no `assumed`: the record adds nothing
        {"who": "caller", "text": "Hey, how are you?", "t": 7.4},
        {"who": "assistant", "text": "Well, thanks!", "t": 9.2},
        {"who": "caller", "text": "no time"},
    ]
    assert end["disclosure"] == "missing" and end["disclosureRepeated"] is True


async def test_the_agent_pages_checkboxes_gate_the_tools(client):
    call = await _start(client, allow={"book": False, "move": True, "answer": False, "greetByName": False})
    assert call["facts"] == "" and call["can"] == {"book": False, "move": True, "answer": False}
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    body = {"start": f"{_tomorrow()}T14:00", "service": "gel", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).json()["detail"]["code"] == "not_allowed"

    paused = await _start(client, room="phone-dev-_p", paused=True)
    pbase = f"/api/v1/internal/voice/calls/{paused['callId']}"
    assert client.post(f"{pbase}/availability", json={"date": _tomorrow()}, headers=W).status_code == 403
    work = {"title": "Quote", "brief": "x"}
    assert client.post(f"{pbase}/start-work", json=work, headers=W).status_code == 403
    assert client.post(f"{pbase}/message", json={"text": "call me back"}, headers=W).json() == {"taken": True}
    end = client.post(f"{pbase}/end", json={"durationSec": 20}, headers=W).json()
    assert end["outcome"] == "message"


async def test_handoff_and_needs_you_outcomes(client):
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    assert client.post(f"{base}/transfer", headers=W).json() == {"transferTo": "+14155550199"}
    assert client.post(f"{base}/end", json={"durationSec": 30}, headers=W).json()["outcome"] == "answered"

    missed = await _start(client, room="phone-dev-_m", handoff={"number": "", "onlyOpenHours": True})
    mbase = f"/api/v1/internal/voice/calls/{missed['callId']}"
    assert client.post(f"{mbase}/transfer", headers=W).json() == {"transferTo": None}
    assert client.post(f"{mbase}/end", json={"durationSec": 30}, headers=W).json()["outcome"] == "handoff_missed"

    unknown = await _start(client, room="phone-dev-_u")
    ubase = f"/api/v1/internal/voice/calls/{unknown['callId']}"
    assert client.post(f"{ubase}/unknown", json={"question": "Do you do bridal parties?"}, headers=W).status_code == 200
    rec = client.post(f"{ubase}/end", json={"durationSec": 30}, headers=W).json()
    assert rec["outcome"] == "needs_you" and rec["steps"][-1]["text"].startswith("Didn't know")

    quiet = await _start(client, room="phone-dev-_s")  # the worker hung up a line nobody spoke on
    qend = client.post(
        f"/api/v1/internal/voice/calls/{quiet['callId']}/end", json={"durationSec": 45, "silent": True}, headers=W
    )
    assert qend.json()["outcome"] == "silent"


async def test_start_work_runs_as_the_owner_and_is_marked_a_contacts(client):
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    work = client.post(f"{base}/start-work", json={"title": "Bridal quote", "brief": "Six people, June."}, headers=W)
    assert work.status_code == 202
    args, kwargs = client.start_work.await_args.args, client.start_work.await_args.kwargs
    assert args[1].room_id == call["callId"] and voice_phone.CONTACT_NOTE in args[1].brief
    assert kwargs["origin"] == "phone_contact"
    run = await messaging_repo.get_voice_run("org_1", args[1].run_id)
    assert run["requesterUserId"] == "user_owner"  # from the line row, never the model
    assert client.post(f"{base}/end", json={"durationSec": 9}, headers=W).json()["outcome"] == "needs_you"


async def test_a_caller_cannot_touch_another_callers_booking(client):
    first = await _start(client)
    day = _tomorrow()
    body = {"start": f"{day}T11:00", "service": "pedicure", "name": "Ana"}
    client.post(f"/api/v1/internal/voice/calls/{first['callId']}/book", json=body, headers=W)
    other = await _start(client, caller="+19998887777", room="phone-dev-_y_2")
    r = client.post(f"/api/v1/internal/voice/calls/{other['callId']}/change", json={"start": f"{day}T11:00"}, headers=W)
    assert r.status_code == 404


async def test_worker_secret_required_and_unknown_number_404(client):
    body = {"calledNumber": NUMBER, "callerNumber": "", "livekitRoom": "r"}
    assert client.post("/api/v1/internal/voice/calls", json=body).status_code == 401
    assert client.post("/api/v1/internal/voice/calls", json=body, headers=W).status_code == 404


async def test_paused_line_takes_messages_only(client):
    call = await _start(client, paused=True)
    assert call["mode"] == "message_only" and call["facts"] == ""
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    body = {"start": f"{_tomorrow()}T14:00", "service": "gel", "name": "Priya"}
    r = client.post(f"{base}/book", json=body, headers=W)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert client.post(f"{base}/message", json={"text": "call me"}, headers=W).status_code == 200
    assert client.post(f"{base}/transfer", headers=W).status_code == 403


async def test_not_live_line_answers_owner_test_call(client):
    body = {"start": f"{_tomorrow()}T14:00", "service": "gel", "name": "Priya"}
    stranger = await _start(client, live=False)
    assert stranger["mode"] == "message_only"
    r = client.post(f"/api/v1/internal/voice/calls/{stranger['callId']}/book", json=body, headers=W)
    assert r.status_code == 403
    owner = await _start(client, caller="+14155550199", room="phone-dev-_o", live=False)  # the handoff number
    assert owner["mode"] == "live"
    assert client.post(f"/api/v1/internal/voice/calls/{owner['callId']}/book", json=body, headers=W).status_code == 200
    fwd = await _start(client, caller="+14155550177", room="phone-dev-_f", live=False, forwardFrom="(415) 555-0177")
    assert fwd["mode"] == "live"


def test_next_time_note_reaches_facts():
    cfg = {**voice_phone.DEFAULTS, "knowledge": [{"q": "", "a": "Parking is behind the building"}]}
    assert "Note: Parking is behind the building" in voice_phone._facts(cfg)


async def _remember(client, call, fact, **extra):
    return client.post(
        f"/api/v1/internal/voice/calls/{call['callId']}/remember", json={"fact": fact, **extra}, headers=W
    )


async def test_returning_caller_memory(client):
    call = await _start(client)
    assert call["callerMemory"] == []
    assert (await _remember(client, call, "Prefers Saturdays")).json() == {"remembered": True}
    client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 20}, headers=W)
    again = await _start(client, room="phone-dev-_x_2")
    assert again["callerMemory"] == ["Prefers Saturdays"]
    row = await voice_phone._seen("org_1", "+14155550133")
    assert row["visits"] == 1
    off = await _start(client, room="phone-dev-_x_3", allow={"greetByName": False})
    assert off["callerMemory"] == []


async def test_remember_limits_and_business_facts(client):
    call = await _start(client)
    assert (await _remember(client, call, "Card 4111 1111 1111 1111")).status_code == 400
    assert (await _remember(client, call, "Card 4111.1111.1111.1111")).status_code == 400
    assert (await _remember(client, call, "x" * 121)).status_code == 400
    for fact in ("Parking is behind the building", "Parking is behind the building"):
        assert (await _remember(client, call, fact, kind="business")).status_code == 200
    assert (await messaging_repo.get_voice_agent("org_1"))["toAdd"] == ["Parking is behind the building"]
    for i in range(12):
        await _remember(client, call, f"Likes {i}")
    client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 20}, headers=W)
    facts = (await voice_phone._seen("org_1", "+14155550133"))["facts"]
    assert len(facts) == 10 and facts[0] == "Likes 11"


async def test_remember_refused_on_a_message_only_line(client):
    paused = await _start(client, room="phone-dev-_p", paused=True)
    assert (await _remember(client, paused, "Prefers Saturdays")).status_code == 403


async def test_forget_during_call_survives_end(client):
    call = await _start(client)
    await _remember(client, call, "Prefers Saturdays")
    from core.dynamodb import run_in_thread, utc_now_iso

    await run_in_thread(
        messaging_repo._table().put_item,
        Item={**voice_phone._caller_key("org_1", "+14155550133"), "facts": [], "forgottenAt": utc_now_iso()},
    )
    client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 5}, headers=W)
    row = await voice_phone._seen("org_1", "+14155550133")
    assert row["facts"] == [] and "name" not in row


async def test_caller_writable_lists_are_capped(client):
    call = await _start(client)
    for i in range(25):
        await _remember(client, call, f"Parking {i}", kind="business")
        await _remember(client, call, f"Likes {i}")
    assert (await messaging_repo.get_voice_agent("org_1"))["toAdd"][0] == "Parking 5"
    assert len((await voice_phone._call(call["callId"]))[1]["remembered"]) == 10


async def test_hours_in_business_timezone(client):
    class Frozen(datetime):  # 17:30 UTC on 10 Nov = 09:30 in Los Angeles, 12:30 in New York
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 10, 17, 30, tzinfo=timezone.utc).astimezone(tz)

    call = await _start(client, timezone="America/Los_Angeles")
    with patch.object(voice_phone, "datetime", Frozen):
        r = client.post(
            f"/api/v1/internal/voice/calls/{call['callId']}/availability",
            json={"date": "2026-11-10", "service": "gel"},
            headers=W,
        )
    slots = r.json()["slots"]
    assert "2026-11-10T09:30" not in slots and "2026-11-10T10:00" in slots and "2026-11-10T18:00" in slots


async def test_overlapping_start_conflicts(client):
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    day = _tomorrow()
    body = {"start": f"{day}T11:00", "service": "gel manicure", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200  # 60 min: 11:00-12:00
    r = client.post(f"{base}/book", json=body | {"start": f"{day}T11:30"}, headers=W)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "slot_taken"
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T12:00"}, headers=W).status_code == 200
    # cancelling frees the cells in the same transaction
    assert client.post(f"{base}/change", json={"start": f"{day}T11:00"}, headers=W).status_code == 200
    assert (
        client.post(f"{base}/book", json=body | {"start": f"{day}T11:30"}, headers=W).status_code == 409
    )  # 12:00 held
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T10:30"}, headers=W).status_code == 200


async def test_a_booking_over_eight_hours_is_refused(client):
    call = await _start(
        client,
        services=[{"name": "Bridal", "minutes": 500, "price": "", "staff": ""}],
        hours={d: {"open": "00:00", "close": "23:59"} for d in DAYS},
    )
    body = {"start": f"{_tomorrow()}T01:00", "service": "bridal", "name": "Priya"}
    r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/book", json=body, headers=W)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "booking_too_long"


async def test_bad_stored_timezone_falls_back(client):
    call = await _start(client, timezone="Mars/Olympus")
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    day = _tomorrow()
    r = client.post(f"{base}/availability", json={"date": day, "service": "gel"}, headers=W)
    assert r.status_code == 200 and f"{day}T10:00" in r.json()["slots"]
    body = {"start": f"{day}T11:00", "service": "gel manicure", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200
    cell = await messaging_repo._get(messaging_repo.owner_pk("org_1"), "VOICECELL#" + _utc(day, "11:00"))
    assert (
        cell["expiresAt"]
        == int(datetime.strptime(_utc(day, "11:00"), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp())
        + 300
        + 86400
    )


def _utc(day: str, hm: str) -> str:  # New York wall time -> the UTC cell key
    from zoneinfo import ZoneInfo

    t = datetime.strptime(f"{day}T{hm}", "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("America/New_York"))
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


async def test_end_emits_outcome_metric(client, monkeypatch):
    """The end of a call emits VoiceCallOutcome tagged with the outcome; a failing emit never fails the route."""
    seen = []
    monkeypatch.setattr(voice_phone, "put_metric", lambda name, **kw: seen.append((name, kw["dimensions"])))
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    assert client.post(url, json={"durationSec": 30}, headers=W).json()["outcome"] == "answered"
    assert seen == [("VoiceCallOutcome", {"outcome": "answered"})]

    def boom(*a, **k):
        raise RuntimeError("emf down")

    monkeypatch.setattr(voice_phone, "put_metric", boom)
    again = await _start(client, room="phone-dev-_x")
    assert (
        client.post(
            f"/api/v1/internal/voice/calls/{again['callId']}/end", json={"durationSec": 5}, headers=W
        ).status_code
        == 200
    )


async def test_inbound_probe_marks_forwarding_ok_and_ends(client):
    """Our own forwarding probe arriving back on the line: marked ok, both legs ended, no greeting, no call record."""
    from core.dynamodb import utc_now_iso
    from core.services import voice_outbound

    probe = {"forwardFrom": "(415) 555-0177", "forwardingCheck": {"startedAt": utc_now_iso(), "callId": "abc"}}
    await voice_phone.put_line(NUMBER, LINE)
    with patch.object(voice_outbound, "end_probe", AsyncMock()) as end:
        for caller, room in ((NUMBER, "phone-dev-_p1"), ("+14155550177", "phone-dev-_p2")):  # either caller id
            await messaging_repo.put_voice_agent("org_1", {**CONFIG, **probe}, "user_owner")
            r = client.post("/api/v1/internal/voice/calls", headers=W,
                            json={"calledNumber": NUMBER, "callerNumber": caller, "livekitRoom": room})  # fmt: skip
            assert r.status_code == 409 and r.json()["detail"]["code"] == "forward_check"
            cfg = await messaging_repo.get_voice_agent("org_1")
            assert cfg["forwardingOk"] is True and cfg["forwardingOkAt"] and "forwardingCheck" not in cfg
            end.assert_awaited_with(room, "abc")
    assert not await voice_phone.messaging_repo.list_owner_rooms("org_1")  # no call record was made
    # the owner's own test call with no check pending is an ordinary call
    owner = await _start(client, caller="+14155550177", room="phone-dev-_p3", forwardFrom="(415) 555-0177")
    assert owner["mode"] == "live"
