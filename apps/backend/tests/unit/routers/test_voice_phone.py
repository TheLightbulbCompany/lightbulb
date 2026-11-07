"""The phone receptionist's backend: a call record per call, Nova's calendar, messages, start_work."""

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
LINE = {
    "ownerId": "org_1",
    "ownerUserId": "user_owner",
    "businessName": "Maya's Nail Studio",
    "assistantName": "Nova",
    "tz": "America/New_York",
    "hours": {"days": [0, 1, 2, 3, 4, 5, 6], "open": "10:00", "close": "19:00"},
    "slotMinutes": 60,
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
        ):
            from main import app

            c = TestClient(app)
            c.start_work = sw
            yield c


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


async def _start(client, caller="+14155550133"):
    await voice_phone.put_line(NUMBER, LINE)
    r = client.post(
        "/api/v1/internal/voice/calls",
        json={"calledNumber": NUMBER, "callerNumber": caller, "livekitRoom": "phone-dev-_x_1"},
        headers=W,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_call_books_takes_a_message_and_leaves_a_record(client):
    call = await _start(client)
    assert call["greeting"].startswith("Thanks for calling Maya's Nail Studio — this is Nova")
    assert "This call is recorded." in call["greeting"]
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    day = _tomorrow()
    slots = client.post(f"{base}/availability", json={"date": day}, headers=W).json()["slots"]
    assert slots[0] == f"{day}T10:00" and slots[-1] == f"{day}T18:00" and len(slots) == 9

    body = {"start": f"{day}T14:00", "service": "gel manicure", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 409  # one booking per slot
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T20:00"}, headers=W).status_code == 400
    assert f"{day}T14:00" not in client.post(f"{base}/availability", json={"date": day}, headers=W).json()["slots"]

    moved = client.post(f"{base}/change", json={"start": f"{day}T14:00", "newStart": f"{day}T15:00"}, headers=W)
    assert moved.status_code == 200 and moved.json()["moved"]
    assert client.post(f"{base}/my-bookings", headers=W).json()["bookings"] == [
        {"start": f"{day}T15:00", "service": "gel manicure"}
    ]

    assert client.post(f"{base}/message", json={"name": "Priya", "text": "bridal party of six?"}, headers=W).json()
    assert client.post(f"{base}/transfer", headers=W).json() == {"transferTo": None}
    work = client.post(f"{base}/start-work", json={"title": "Bridal quote", "brief": "Six people, June."}, headers=W)
    assert work.status_code == 202
    sent = client.start_work.await_args.args[1]
    assert sent.room_id == call["callId"] and voice_phone.CONTACT_NOTE in sent.brief
    run = await messaging_repo.get_voice_run("org_1", sent.run_id)
    assert run["requesterUserId"] == "user_owner"  # from the line row, never the model

    end = client.post(
        f"{base}/end",
        json={"durationS": 160, "summary": "Booked Priya.", "transcript": [{"role": "user", "text": "hi"}]},
        headers=W,
    ).json()
    assert end["outcome"] == "booked" and end["durationS"] == 160 and end["status"] == "ended"
    meta = await messaging_repo.get_room(call["callId"])
    assert meta["hidden"] and meta["call"]["callerNumber"] == "+14155550133"
    assert (
        meta["call"]["messages"][0]["text"] == "bridal party of six?" and meta["call"]["transcript"][0]["text"] == "hi"
    )


async def test_a_caller_cannot_touch_another_callers_booking(client):
    first = await _start(client)
    day = _tomorrow()
    body = {"start": f"{day}T11:00", "service": "pedicure", "name": "Ana"}
    client.post(f"/api/v1/internal/voice/calls/{first['callId']}/book", json=body, headers=W)
    other = client.post(
        "/api/v1/internal/voice/calls",
        json={"calledNumber": NUMBER, "callerNumber": "+19998887777", "livekitRoom": "phone-dev-_y_2"},
        headers=W,
    ).json()
    r = client.post(f"/api/v1/internal/voice/calls/{other['callId']}/change", json={"start": f"{day}T11:00"}, headers=W)
    assert r.status_code == 404


async def test_worker_secret_required_and_unknown_number_404(client):
    body = {"calledNumber": NUMBER, "callerNumber": "", "livekitRoom": "r"}
    assert client.post("/api/v1/internal/voice/calls", json=body).status_code == 401
    assert client.post("/api/v1/internal/voice/calls", json=body, headers=W).status_code == 404
