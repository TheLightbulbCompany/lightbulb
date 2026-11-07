"""The phone receptionist's backend: the call record the owner's Home reads, the Agent page's settings gating the tools,
the assistant's calendar, messages, hand-off and start_work."""

from datetime import datetime, timedelta
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
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 400  # taken
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T14:30"}, headers=W).status_code == 400  # overlaps
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
