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


class _WorkerClient(TestClient):
    """Posts as the phone worker does: a per-call route carries the callToken its start returned (M4)."""

    tokens: dict

    def post(self, url, *args, headers=None, **kwargs):
        parts = url.split("/")  # /api/v1/internal/voice/calls/<id>/<route>
        call_id = parts[6] if len(parts) > 7 and url.startswith("/api/v1/internal/voice/calls/") else None
        if headers is not None and call_id in self.tokens and voice_phone.CALL_TOKEN_HEADER not in headers:
            headers = {**headers, voice_phone.CALL_TOKEN_HEADER: self.tokens[call_id]}
        r = super().post(url, *args, headers=headers, **kwargs)
        if url == "/api/v1/internal/voice/calls" and r.status_code == 200:
            self.tokens[r.json()["callId"]] = r.json()["callToken"]
        return r


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
            patch.object(voice_phone.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)),
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

            c = _WorkerClient(app)
            c.tokens = {}
            c.start_work = sw
            yield c


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


async def _start(client, caller="+14155550133", room="phone-dev-_x_1", ago=3600, **config):
    """A started call, begun `ago` seconds back (the billed duration is capped by the time since start, M4)."""
    await voice_phone.put_line(NUMBER, LINE)
    await messaging_repo.put_voice_agent("org_1", {**CONFIG, **config}, "user_owner")
    r = client.post(
        "/api/v1/internal/voice/calls",
        json={"calledNumber": NUMBER, "callerNumber": caller, "livekitRoom": room},
        headers=W,
    )
    assert r.status_code == 200, r.text
    if ago:
        meta = await messaging_repo.get_room(r.json()["callId"])
        began = datetime.fromisoformat(meta["call"]["startedAt"]) - timedelta(seconds=ago)
        await messaging_repo.patch_room_meta(
            meta["roomId"], {"call": {**meta["call"], "startedAt": began.isoformat()}},
            expected_revision=int(meta.get("revision", 0)),
        )  # fmt: skip
    return r.json()


async def test_a_call_books_moves_and_leaves_the_record_home_reads(client):
    call = await _start(client)
    assert call["greeting"] == (
        "Hi, I'm Nova, Maya's Nail Studio's AI assistant. How can I help?"
    )  # one short sentence and its question (8 Nov prod: a long opening buried the caller's first line)
    assert "Gel manicure $45 (60 min)" in call["facts"] and "Behind the building." in call["facts"]
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    day = _tomorrow()
    slots = client.post(f"{base}/availability", json={"date": day, "service": "gel"}, headers=W).json()["slots"]
    assert slots[0] == f"{day}T10:00" and slots[-1] == f"{day}T18:00" and len(slots) == 17  # half-hourly

    body = {"start": f"{day}T14:00", "service": "gel manicure", "name": "Priya"}
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200
    assert client.post(f"{base}/book", json=body, headers=W).status_code == 200  # a retry returns the same booking
    assert client.post(f"{base}/book", json=body | {"name": "Ana"}, headers=W).status_code == 409  # taken
    assert client.post(f"{base}/book", json=body | {"start": f"{day}T14:30"}, headers=W).status_code == 409  # overlaps
    assert (
        client.post(f"{base}/book", json=body | {"start": f"{day}T18:30"}, headers=W).status_code == 400
    )  # past close
    moved = client.post(f"{base}/change", json={"start": f"{day}T14:00", "newStart": f"{day}T14:30"}, headers=W)
    assert moved.status_code == 200 and moved.json()["moved"]  # its own old slot doesn't block the move
    mine = client.post(f"{base}/my-bookings", headers=W).json()["bookings"]
    assert [{k: b[k] for k in ("start", "service")} for b in mine] == [
        {"start": f"{day}T14:30", "service": "gel manicure"}
    ]
    assert mine[0]["bookingId"]  # what /change takes when two bookings share a start
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


async def test_the_owners_greeting_is_used_as_written(client):
    mine = "Hey, Maya's, Nova speaking, what can I do?"  # no AI words, no name check: the business owns its opening
    assert (await _start(client, greeting=mine))["greeting"] == mine


async def test_pending_voice_payment_cannot_use_owner_test_bypass_or_business_tools(client):
    with patch.object(
        voice_phone.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"metronome_voice_contract_id": "voice", "voice_plan_status": "pending_payment"}),
    ):
        call = await _start(client, caller=CONFIG["handoff"]["number"])
        assert call["mode"] == "message_only" and call["forwardTo"] is None
        assert not any(call["can"].values())
        result = client.post(
            f"/api/v1/internal/voice/calls/{call['callId']}/availability", json={"date": _tomorrow()}, headers=W
        )
        assert result.status_code == 403


async def test_the_record_keeps_each_lines_time(client):
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    tx = [
        {"who": "assistant", "text": "Thanks for calling.", "t": 1.1, "assumed": True},
        {"who": "caller", "text": "Hey, how are you?", "t": 7.4},
        {"who": "assistant", "text": "Well, thanks!", "t": 9.25},
        {"who": "caller", "text": "no time", "t": "soon"},
    ]
    body = {"durationSec": 12, "transcript": tx}
    end = client.post(url, json=body, headers=W).json()
    assert end["transcript"] == [
        {"who": "assistant", "text": "Thanks for calling.", "t": 1.1},  # no `assumed`: the record adds nothing
        {"who": "caller", "text": "Hey, how are you?", "t": 7.4},
        {"who": "assistant", "text": "Well, thanks!", "t": 9.2},
        {"who": "caller", "text": "no time"},
    ]


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


async def test_unknown_creates_question_and_to_add(client):
    """Didn't know: the question waits in the Agent page's `toAdd`, and the call ends with a per-call task whose
    Question (subkind `didnt_know`) carries the caller's own words, so answering it resumes that task."""
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    q = "Do you do bridal parties?"
    assert client.post(f"{base}/unknown", json={"question": q}, headers=W).status_code == 200
    assert client.post(f"{base}/unknown", json={"question": q}, headers=W).status_code == 200  # a retried tool call
    assert (await messaging_repo.get_voice_agent("org_1"))["toAdd"] == [q]
    assert client.post(f"{base}/end", json={"durationSec": 30}, headers=W).json()["outcome"] == "needs_you"
    [(args, kwargs)] = [(c.args, c.kwargs) for c in client.start_work.await_args_list]
    assert args[0] == "org_1" and args[1].title == f"Answer +14155550133: {q}" and kwargs["origin"] == "phone_contact"
    assert voice_phone.CONTACT_NOTE in args[1].brief and q in args[1].brief
    ask = kwargs["ask"]
    assert ask["kind"] == "question" and ask["subkind"] == "didnt_know" and ask["blocking"] is True
    assert ask["preview"] == {
        "kind": "didnt_know",
        "roomId": call["callId"],
        "caller": "+14155550133",
        "question": q,
        "firstName": "",
    }
    assert [o["id"] for o in ask["options"]] == ["answer", "call_myself", "done"]
    run = await messaging_repo.get_voice_run("org_1", args[1].run_id)
    assert run["requesterUserId"] == "user_owner" and run["roomId"] == call["callId"]


async def test_handoff_missed_creates_question(client):
    missed = await _start(client, room="phone-dev-_h", handoff={"number": "", "onlyOpenHours": True})
    base = f"/api/v1/internal/voice/calls/{missed['callId']}"
    assert client.post(f"{base}/transfer", headers=W).json() == {"transferTo": None}
    assert client.post(f"{base}/end", json={"durationSec": 30}, headers=W).json()["outcome"] == "handoff_missed"
    ask = client.start_work.await_args.kwargs["ask"]
    assert ask["subkind"] == "handoff" and ask["preview"]["roomId"] == missed["callId"]
    assert [o["id"] for o in ask["options"]] == ["call_myself", "done"]

    failed = await _start(client, room="phone-dev-_f")
    fbase = f"/api/v1/internal/voice/calls/{failed['callId']}"
    assert client.post(f"{fbase}/transfer", json={"failed": True}, headers=W).json() == {"transferTo": None}
    client.post(f"{fbase}/end", json={"durationSec": 30}, headers=W)
    assert client.start_work.await_args.kwargs["ask"]["subkind"] == "handoff"  # line_problem is for system issues


async def test_to_add_is_capped_oldest_first(client):
    """Caller text: at most TO_ADD_MAX questions of TO_ADD_CHARS each reach the Agent page; the oldest drop."""
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    for i in range(voice_phone.TO_ADD_MAX + 5):
        assert client.post(f"{base}/unknown", json={"question": f"q{i} " + "x" * 400}, headers=W).status_code == 200
    to_add = (await messaging_repo.get_voice_agent("org_1"))["toAdd"]
    assert len(to_add) == voice_phone.TO_ADD_MAX and to_add[0].startswith("q5 ") and to_add[-1].startswith("q24 ")
    assert all(len(t) == voice_phone.TO_ADD_CHARS for t in to_add)


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


# --- staff capacity: one booking per free person at a time (board 3.23) ------------------------------------------
# Each person has a stable id (kept on rename); their keys are VOICEBOOK#<start>#<id> and VOICECELL#<utc>#<id>.

ANA, MEI = {"id": "a1", "name": "Ana"}, {"id": "m1", "name": "Mei"}
STAFFED = {
    "staff": [ANA, MEI],
    "services": [
        {"name": "Gel manicure", "minutes": 60, "price": "$45", "staff": ""},  # anyone
        {"name": "Pedicure", "minutes": 60, "price": "$40", "staff": "m1"},  # only Mei
    ],
}


def _book_as(client, call, start, service="gel manicure", name="Priya"):
    return client.post(
        f"/api/v1/internal/voice/calls/{call['callId']}/book",
        json={"start": start, "service": service, "name": name},
        headers=W,
    )


async def _row(start: str, lane: str = "") -> dict | None:
    return await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICEBOOK#{start}{lane}")


def _slots(client, call, service: str) -> list[str]:
    r = client.post(
        f"/api/v1/internal/voice/calls/{call['callId']}/availability",
        json={"date": _tomorrow(), "service": service},
        headers=W,
    )
    return r.json()["slots"]


async def _staff_now(staff: list[dict]) -> None:  # the owner edits the staff list on the Agent page
    await messaging_repo.put_voice_agent("org_1", {**CONFIG, **STAFFED, "staff": staff}, "user_owner")


async def test_two_staff_take_two_bookings_at_once_and_refuse_a_third(client):
    calls = [await _start(client, caller=f"+1415555013{i}", room=f"phone-dev-_s{i}", **STAFFED) for i in range(3)]
    at = f"{_tomorrow()}T11:00"
    first, second = _book_as(client, calls[0], at), _book_as(client, calls[1], at)
    assert first.status_code == second.status_code == 200
    assert {first.json()["staff"], second.json()["staff"]} == {"Ana", "Mei"}
    third = _book_as(client, calls[2], f"{_tomorrow()}T11:30")  # overlaps both
    assert third.status_code == 409 and third.json()["detail"]["code"] == "slot_taken"
    assert (await _row(at, "#a1"))["staff"] == "Ana" and (await _row(at, "#m1"))["staff"] == "Mei"
    assert await _row(at) is None  # with staff, nothing takes the unsuffixed (no-staff) keys
    slots = _slots(client, calls[2], "gel")
    assert at not in slots and f"{_tomorrow()}T12:00" in slots


async def test_a_service_one_person_does_books_only_that_persons_time(client):
    a = await _start(client, caller="+14155550141", room="phone-dev-_r1", **STAFFED)
    b = await _start(client, caller="+14155550142", room="phone-dev-_r2", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    took = _book_as(client, a, at, service="pedicure")
    assert took.status_code == 200 and took.json()["staff"] == "Mei" and await _row(at, "#m1")
    assert _book_as(client, b, at, service="pedicure").status_code == 409  # Ana is free but doesn't do pedicures
    assert at not in _slots(client, b, "pedicure") and at in _slots(client, b, "gel")
    r = _book_as(client, b, at)
    assert r.json()["staff"] == "Ana"  # an anyone-service still gets Ana


async def test_cancel_frees_the_person_it_took(client):
    a = await _start(client, caller="+14155550151", room="phone-dev-_c1", **STAFFED)
    b = await _start(client, caller="+14155550152", room="phone-dev-_c2", **STAFFED)
    c = await _start(client, caller="+14155550153", room="phone-dev-_c3", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at).json()["staff"] == "Ana"
    assert _book_as(client, b, at).json()["staff"] == "Mei"
    assert _book_as(client, c, at, service="pedicure").status_code == 409
    r = client.post(f"/api/v1/internal/voice/calls/{b['callId']}/change", json={"start": at}, headers=W)
    assert r.status_code == 200 and r.json()["cancelled"] and await _row(at, "#m1") is None
    assert await _row(at, "#a1")  # Ana's booking stands
    assert _book_as(client, c, at, service="pedicure").json()["staff"] == "Mei"


async def test_a_move_keeps_the_person(client):
    a = await _start(client, caller="+14155550161", room="phone-dev-_m1", **STAFFED)
    at, to = f"{_tomorrow()}T11:00", f"{_tomorrow()}T14:00"
    assert _book_as(client, a, at, service="pedicure").json()["staff"] == "Mei"
    r = client.post(f"/api/v1/internal/voice/calls/{a['callId']}/change", json={"start": at, "newStart": to}, headers=W)
    assert r.status_code == 200 and await _row(at, "#m1") is None and (await _row(to, "#m1"))["staff"] == "Mei"
    mine = client.post(f"/api/v1/internal/voice/calls/{a['callId']}/my-bookings", headers=W).json()["bookings"]
    assert [{k: b[k] for k in ("start", "service", "staff")} for b in mine] == [
        {"start": to, "service": "pedicure", "staff": "Mei"}
    ]


async def test_removing_someone_never_lets_another_person_be_double_booked(client):
    """The reviewer's case: Mei booked, Ana removed; Mei's own keys still hold her time."""
    a = await _start(client, caller="+14155550191", room="phone-dev-_x1", **STAFFED)
    b = await _start(client, caller="+14155550192", room="phone-dev-_x2", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at, service="pedicure").json()["staff"] == "Mei"
    await _staff_now([MEI])
    assert at not in _slots(client, b, "pedicure") and at not in _slots(client, b, "gel")
    assert _book_as(client, b, at, service="pedicure").status_code == 409
    assert _book_as(client, b, f"{_tomorrow()}T11:30").status_code == 409


async def test_reordering_the_list_keeps_who_is_booked(client):
    a = await _start(client, caller="+14155550193", room="phone-dev-_x3", **STAFFED)
    b = await _start(client, caller="+14155550194", room="phone-dev-_x4", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at, service="pedicure").json()["staff"] == "Mei"
    await _staff_now([MEI, ANA])
    assert _book_as(client, b, at, service="pedicure").status_code == 409
    assert _book_as(client, b, at).json()["staff"] == "Ana"
    assert _book_as(client, b, at, name="Bo").status_code == 409  # full (the same body again is a retry)


async def test_a_rename_keeps_the_persons_bookings(client):
    a = await _start(client, caller="+14155550195", room="phone-dev-_x5", **STAFFED)
    b = await _start(client, caller="+14155550196", room="phone-dev-_x6", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at, service="pedicure").json()["staff"] == "Mei"
    await _staff_now([ANA, {"id": "m1", "name": "May"}])
    assert _book_as(client, b, at, service="pedicure").status_code == 409  # May is the same person
    took = _book_as(client, b, f"{_tomorrow()}T14:00", service="pedicure")
    assert took.json()["staff"] == "May" and await _row(f"{_tomorrow()}T14:00", "#m1")


async def test_no_staff_list_keeps_todays_keys_and_one_at_a_time(client):
    a = await _start(client, caller="+14155550171", room="phone-dev-_o1")
    b = await _start(client, caller="+14155550172", room="phone-dev-_o2")
    day, at = _tomorrow(), f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at).status_code == 200
    row = await _row(at)
    assert "staff" not in row and row["cells"][0] == "VOICECELL#" + _utc(day, "11:00")
    assert _book_as(client, b, at).status_code == 409


async def test_a_booking_from_before_staff_takes_one_persons_capacity(client):
    """An unsuffixed booking is one person's time, nobody knows whose: with two people, one more fits, not two."""
    calls = [await _start(client, caller=f"+1415555020{i}", room=f"phone-dev-_l{i}") for i in range(4)]
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, calls[0], at).status_code == 200  # no staff yet: the unsuffixed keys
    await _staff_now([ANA, MEI])
    assert at in _slots(client, calls[1], "gel")
    assert _book_as(client, calls[1], at, service="pedicure").json()["staff"] == "Mei"
    assert at not in _slots(client, calls[2], "gel")  # Ana is free, but the old booking holds someone
    assert _book_as(client, calls[2], at).status_code == 409
    assert _book_as(client, calls[3], f"{_tomorrow()}T11:30").status_code == 409
    assert _book_as(client, calls[3], f"{_tomorrow()}T12:00").json()["staff"] in ("Ana", "Mei")


async def test_a_concurrent_double_book_on_the_last_person_lets_exactly_one_win(client):
    a = await _start(client, caller="+14155550181", room="phone-dev-_k1", **STAFFED)
    b = await _start(client, caller="+14155550182", room="phone-dev-_k2", **STAFFED)
    cfg = await voice_phone.agent_config("org_1")
    at = f"{_tomorrow()}T11:00"
    real, raced = messaging_repo._transact, []

    async def interleaved(items):
        # Both callers saw Mei free; the other caller's write lands between this one's choice and its write.
        if not raced:
            raced.append(True)
            await voice_phone._book("org_1", b["callId"], {"caller": "+14155550182"}, cfg, at, "pedicure", "Bo")
        return await real(items)

    with patch.object(messaging_repo, "_transact", interleaved):
        with pytest.raises(voice_phone.HTTPException) as lost:
            await voice_phone._book("org_1", a["callId"], {"caller": "+14155550181"}, cfg, at, "pedicure", "Al")
    assert lost.value.status_code == 409 and lost.value.detail["code"] == "slot_taken"
    assert (await _row(at, "#m1"))["name"] == "Bo" and await _row(at) is None


async def test_concurrent_bookings_beside_an_old_booking_never_exceed_capacity(client):
    """Old booking + two people = room for one more. Two callers race for it: exactly one wins."""
    calls = [await _start(client, caller=f"+1415555021{i}", room=f"phone-dev-_q{i}") for i in range(3)]
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, calls[0], at).status_code == 200
    await _staff_now([ANA, MEI])
    cfg = await voice_phone.agent_config("org_1")
    real, raced = messaging_repo._transact, []

    async def interleaved(items):
        if not raced:
            raced.append(True)
            await voice_phone._book("org_1", calls[2]["callId"], {"caller": "+14155550212"}, cfg, at, "gel", "Bo")
        return await real(items)

    with patch.object(messaging_repo, "_transact", interleaved):
        with pytest.raises(voice_phone.HTTPException) as lost:
            await voice_phone._book("org_1", calls[1]["callId"], {"caller": "+14155550211"}, cfg, at, "gel", "Al")
    assert lost.value.detail["code"] == "slot_taken"
    held = [await _row(at, lane) for lane in ("#a1", "#m1")]
    assert sum(r is not None for r in held) == 1


async def test_clearing_the_roster_keeps_counting_the_staffs_bookings(client):
    """Capacity is per slot across every lane: two staffed bookings, then no staff (capacity 1), is full."""
    calls = [await _start(client, caller=f"+1415555022{i}", room=f"phone-dev-_z{i}", **STAFFED) for i in range(3)]
    at = f"{_tomorrow()}T11:00"
    assert _book_as(client, calls[0], at).status_code == _book_as(client, calls[1], at).status_code == 200
    await _staff_now([])
    assert at not in _slots(client, calls[2], "gel")
    assert _book_as(client, calls[2], at).status_code == 409
    for c in calls[:1]:  # one cancel frees one unit: still 1 booked, capacity 1, full
        assert client.post(f"/api/v1/internal/voice/calls/{c['callId']}/change", json={"start": at}, headers=W).json()[
            "cancelled"
        ]
    assert _book_as(client, calls[2], at).status_code == 409
    client.post(f"/api/v1/internal/voice/calls/{calls[1]['callId']}/change", json={"start": at}, headers=W)
    assert at in _slots(client, calls[2], "gel")
    assert _book_as(client, calls[2], at).status_code == 200 and "staff" not in await _row(at)  # back to no-staff keys


async def test_a_retry_returns_the_booking_instead_of_taking_another_person(client):
    a = await _start(client, caller="+14155550231", room="phone-dev-_i1", **STAFFED)
    at = f"{_tomorrow()}T11:00"
    first, again = _book_as(client, a, at), _book_as(client, a, at)
    assert first.status_code == again.status_code == 200 and first.json()["staff"] == again.json()["staff"] == "Ana"
    assert await _row(at, "#m1") is None  # Mei is still free


def test_an_empty_service_is_the_whole_roster():
    cfg = {"staff": [ANA, MEI], "services": [{"name": "Pedicure", "minutes": 60, "staff": "m1"}]}
    assert voice_phone._lanes(cfg, "") == [("#a1", "Ana"), ("#m1", "Mei")]
    assert voice_phone._lanes(cfg, "pedicure") == [("#m1", "Mei")]


async def test_a_booking_that_lands_first_does_not_refuse_a_free_person(client):
    """A Mei-only booking lands between an anyone booking's read and its write: the stale guards fail that write,
    capacity is re-read, and Ana (still free) is booked rather than slot_taken."""
    a = await _start(client, caller="+14155550271", room="phone-dev-_t1", **STAFFED)
    b = await _start(client, caller="+14155550272", room="phone-dev-_t2", **STAFFED)
    cfg = await voice_phone.agent_config("org_1")
    at = f"{_tomorrow()}T11:00"
    real, raced = messaging_repo._transact, []

    async def interleaved(items):
        if not raced:
            raced.append(True)
            await voice_phone._book("org_1", b["callId"], {"caller": "+14155550272"}, cfg, at, "pedicure", "Bo")
        return await real(items)

    with patch.object(messaging_repo, "_transact", interleaved):
        row = await voice_phone._book("org_1", a["callId"], {"caller": "+14155550271"}, cfg, at, "gel", "Al")
    assert row["staff"] == "Ana" and (await _row(at, "#m1"))["name"] == "Bo"


async def test_two_identical_books_at_once_make_one_booking(client):
    """The same call books the same thing twice at once: both pass the duplicate read, but the second's write meets
    the first's idempotency row and returns that booking instead of taking Mei."""
    a = await _start(client, caller="+14155550281", room="phone-dev-_u1", **STAFFED)
    cfg = await voice_phone.agent_config("org_1")
    at, real, raced, inner = f"{_tomorrow()}T11:00", messaging_repo._transact, [], []
    args = ("org_1", a["callId"], {"caller": "+14155550281"}, cfg, at, "gel", "Priya")

    async def interleaved(items):
        if not raced:
            raced.append(True)
            inner.append(await voice_phone._book(*args))
        return await real(items)

    with patch.object(messaging_repo, "_transact", interleaved):
        outer = await voice_phone._book(*args)
    assert inner[0]["staff"] == outer["staff"] == "Ana" and outer["_repeat"]
    assert outer["bookingId"] == inner[0]["bookingId"] and await _row(at, "#m1") is None
    # Once that booking is cancelled, the same booking can be made again.
    assert client.post(f"/api/v1/internal/voice/calls/{a['callId']}/change", json={"start": at}, headers=W).json()[
        "cancelled"
    ]
    assert _book_as(client, a, at).json()["staff"] == "Ana"


async def test_with_google_one_shared_calendar_holds_one_booking_at_a_time(client):
    """Bookings that go to Google share one calendar: a held booking (its Google write still pending) blocks the
    slot for every person."""
    a = await _start(client, caller="+14155550251", room="phone-dev-_g1", **STAFFED, bookingsGo="google")
    cfg = await voice_phone.agent_config("org_1")
    at = f"{_tomorrow()}T11:00"
    assert voice_phone._capacity(cfg) == 1 and voice_phone._capacity({**cfg, "bookingsGo": "own"}) == 2
    held = await voice_phone._book("org_1", a["callId"], {"caller": "+14155550251"}, cfg, at, "gel", "Al")
    assert held["bookingState"] == "held"
    with pytest.raises(voice_phone.HTTPException) as refused:
        await voice_phone._book("org_1", "other-call", {"caller": "+14155550252"}, cfg, at, "gel", "Bo")
    assert refused.value.detail["code"] == "slot_taken"
    assert at not in voice_phone.open_starts(
        cfg,
        at[:10],
        await voice_phone._taken("org_1", at[:10]),
        voice_phone._now(cfg),
        60,
        voice_phone._lanes(cfg, "gel"),
    )


async def test_two_bookings_at_one_start_are_changed_by_id(client):
    a = await _start(client, caller="+14155550261", room="phone-dev-_d1", **STAFFED)
    base, at = f"/api/v1/internal/voice/calls/{a['callId']}", f"{_tomorrow()}T11:00"
    assert _book_as(client, a, at, name="Priya").status_code == _book_as(client, a, at, name="Sam").status_code == 200
    r = client.post(f"{base}/change", json={"start": at}, headers=W)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ambiguous_booking"
    options = r.json()["detail"]["options"]
    assert sorted(o["name"] for o in options) == ["Priya", "Sam"] and all(o["bookingId"] for o in options)
    sam = next(o for o in options if o["name"] == "Sam")
    gone = client.post(f"{base}/change", json={"start": at, "bookingId": sam["bookingId"]}, headers=W)
    assert gone.status_code == 200 and gone.json()["cancelled"] and gone.json()["name"] == "Sam"
    assert client.post(f"{base}/change", json={"start": at}, headers=W).json()["name"] == "Priya"  # unambiguous now
    wrong = client.post(f"{base}/change", json={"start": at, "bookingId": "nope"}, headers=W)
    assert wrong.status_code == 404


async def test_a_move_keeps_the_slot_totals_right(client):
    calls = [await _start(client, caller=f"+1415555024{i}", room=f"phone-dev-_v{i}", **STAFFED) for i in range(4)]
    at, to = f"{_tomorrow()}T11:00", f"{_tomorrow()}T14:00"
    assert _book_as(client, calls[0], at).json()["staff"] == "Ana"
    assert _book_as(client, calls[1], at).json()["staff"] == "Mei"
    moved = client.post(
        f"/api/v1/internal/voice/calls/{calls[1]['callId']}/change", json={"start": at, "newStart": to}, headers=W
    )
    assert moved.status_code == 200
    assert _book_as(client, calls[2], at).json()["staff"] == "Mei"  # 11:00 has one unit back
    assert _book_as(client, calls[3], to).json()["staff"] == "Ana"  # 14:00: Mei moved there, Ana is free
    assert _book_as(client, calls[3], to, name="Bo").status_code == 409


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


async def test_minutes_used_up_and_run_out_forward_hands_the_call_to_the_owner(client):
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="forward")
    assert call["forwardTo"] == "+14155550199"
    rec = (await messaging_repo.get_room(call["callId"]))["call"]
    assert rec["outcome"] == "answered" and "transferredTo" not in rec  # nothing said until the transfer connects
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    rec = client.post(url, json={"durationSec": 0, "forwarded": True}, headers=W).json()
    assert rec["outcome"] == "forwarded" and rec["transferredTo"] == "+14155550199"
    assert rec["steps"][-1]["text"] == "Minutes used up: forwarded to you"


async def test_a_failed_run_out_forward_is_an_ordinary_answered_call(client):
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="forward")
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    rec = client.post(url, json={"durationSec": 90}, headers=W).json()  # the worker answered instead
    assert rec["outcome"] == "answered" and "transferredTo" not in rec and not rec["steps"]


@pytest.mark.parametrize("line", [{"forwardFrom": "(415) 555-0199"}, {"handoff": {"number": NUMBER}}])
async def test_run_out_never_forwards_to_the_line_that_rings_us(client, line):
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="forward", **line)
    assert call["forwardTo"] is None


async def test_call_end_adds_to_the_months_counter_once(client):
    from core.billing import voice_billing

    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    client.post(url, json={"durationSec": 160}, headers=W)
    client.post(url, json={"durationSec": 160}, headers=W)  # a retried end
    assert await voice_billing.counted_seconds("org_1", None) == 160


async def test_minutes_used_up_and_run_out_keep_answers(client):
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="keep")
    assert call["forwardTo"] is None and call["mode"] == "live"


async def test_call_end_reports_minutes_to_metronome(client):
    from core.billing import voice_billing

    with (
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)),
    ):
        call = await _start(client)
        client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 160}, headers=W)
    row = await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICEUSAGE#{call['callId']}")
    event = row["voiceUsageEvent"]
    assert event["transactionId"] == call["callId"] and event["customerId"] == "org_1"
    assert event["seconds"] == 160 and event["kind"] == "phone"
    assert event["properties"] == {"seconds": "160", "kind": "phone"}
    assert event["timestamp"].endswith("Z")
    pointer = await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"WORK#VOICEUSAGE#{call['callId']}")
    assert pointer["payload"] == {"kind": "voice_usage", "ownerId": "org_1", "transactionId": call["callId"]}


async def test_failed_phone_usage_admission_retries_with_original_ended_duration(client):
    from fastapi import HTTPException
    from core.billing import voice_billing

    call = await _start(client)
    original = await messaging_repo.get_room(call["callId"])
    original_start = original["call"]["startedAt"]
    with patch.object(
        voice_billing,
        "report_minutes",
        AsyncMock(side_effect=[HTTPException(status_code=503), None]),
    ) as report:
        first = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 60}, headers=W)
        retry = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 900}, headers=W)
    assert first.status_code == 503 and retry.status_code == 200
    assert report.await_args_list[0].args == report.await_args_list[1].args
    assert report.await_args_list[0].args[2:] == (original_start, 60, "phone")


async def test_a_failed_minutes_counter_still_ends_the_call(client):
    from core.billing import voice_billing

    call = await _start(client)
    with patch.object(voice_billing, "add_month_seconds", AsyncMock(side_effect=RuntimeError("ddb down"))):
        r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 60}, headers=W)
    assert r.status_code == 200 and r.json()["status"] == "ended"


async def test_a_failed_run_out_check_answers_the_call(client):
    with patch.object(voice_phone, "month_seconds", AsyncMock(side_effect=RuntimeError("ddb down"))):
        call = await _start(client, runOut="forward")
    assert call["forwardTo"] is None and call["mode"] == "live"


# --- the caller's lines go to the Router (one-brain plan, "Phone, too") ---------------------------------------
FRONT = {"kind": "agent", "id": "agent_front", "displayName": "Lightbulb", "mentionHandle": "lightbulb",
         "role": "member", "listenMode": "mentions"}  # fmt: skip


@pytest.fixture
def router_seat():
    with (
        patch.object(voice_phone.task_conversation, "_front_agent", AsyncMock(return_value=FRONT)),
        patch.object(voice_phone.voice_calls, "prewake_owner") as wake,
        patch.object(voice_phone.admission, "enqueue_trigger", AsyncMock(return_value=True)) as enq,
    ):
        yield wake, enq


async def test_a_call_seats_lightbulb_and_prewarms_the_router(client, router_seat):
    wake, _ = router_seat
    call = await _start(client)
    members = await messaging_repo.list_members(call["callId"])
    assert {(m["kind"], m["id"]) for m in members} == {("human", "user_owner"), ("agent", "agent_front")}
    assert (await messaging_repo.get_room(call["callId"]))["frontAgentId"] == FRONT["id"]
    wake.assert_called_once_with("org_1", team=[FRONT])
    assert call["after"] == 0


async def test_a_run_out_forward_prewarms_nothing(client, router_seat):
    wake, _ = router_seat
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="forward")
    assert call["forwardTo"] == "+14155550199" and not wake.called


async def test_a_caller_line_is_a_caller_principal_trigger_for_the_router(client, router_seat):
    _, enq = router_seat
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/turn"
    out = client.post(url, json={"n": 0, "text": "How much is a gel manicure?"}, headers=W).json()
    again = client.post(url, json={"n": 0, "text": "How much is a gel manicure?"}, headers=W).json()
    assert again["messageId"] == out["messageId"]  # a retried post is the same message
    msg = await messaging_repo.get_message_by_id(call["callId"], out["messageId"])
    assert msg["text"] == "How much is a gel manicure?"
    assert msg["author"] == {"kind": "human", "id": f"caller:{call['callId']}", "displayName": "Caller +14155550133"}
    ws, trig = enq.await_args.args
    assert ws == "org_1" and enq.await_args.kwargs == {"room_id": call["callId"]}
    assert trig.source == "voice" and trig.admission == "respond" and trig.lane == "shared"
    assert trig.actor_user_id == "user_owner"  # the line's owner user, never the model or the caller
    assert trig.ref["origin"] == "phone_contact" and trig.ref["callId"] == call["callId"]
    assert trig.ref["roomId"] == call["callId"] and trig.ref["messageId"] == out["messageId"]
    assert trig.ref["text"].startswith("How much is a gel manicure?") and "outside contact" in trig.ref["text"]
    assert "Offer at most three open times" in trig.ref["text"]


T1 = "7e8bf6dd-9efb-52b8-ad56-6ed3f06754c0"


async def _agent_line(cid: str, text: str, key: str, refs: list[dict] | None = None) -> None:
    """A Lightbulb line in the call room (the Router's ack, a thread's answer), as `post_as_lightbulb` writes it."""
    await messaging_repo.create_message(
        room_id=cid, owner_id="org_1", idemp_pk=messaging_repo.owner_pk("org_1"), idemp_sk=key, req_hash=key,
        author={"kind": "agent", "id": "agent_front", "displayName": "Lightbulb"}, text=text, mentions=[],
        reply_to_message_id=None, thread_root_message_id=None, run_id=None, dispatch_targets=[],
        source_agent_id=None, client_message_id=None, author_member_check=False,
        attrs={"taskRefs": refs} if refs else None,
    )  # fmt: skip


async def _thread_in_call(cid: str, status: str) -> dict:
    """The Router's ack in the call room, with the caller thread it started as its card."""
    await _agent_line(cid, "Vera is checking Saturday.", "ack-1", [{"taskId": T1, "role": "started"}])
    return {"taskId": T1, "origin": "phone_contact", "status": status, "audience": {"kind": "room", "roomId": cid}}


async def test_a_callers_next_line_goes_to_the_calls_open_thread_not_a_new_one(client, router_seat):
    """8 Nov prod final smoke, book-112005: "The first time you said works for me" and "Yes, that's right" each
    started a new Front desk thread with no history; the "yes" thread asked again and picked 12:00 PM, a time never
    offered. The call's open thread gets the line, as a typed reply in a task's thread does."""
    _, enq = router_seat
    call = await _start(client)
    cid, url = call["callId"], f"/api/v1/internal/voice/calls/{call['callId']}/turn"
    client.post(url, json={"n": 0, "text": "Anything Saturday afternoon?"}, headers=W)
    assert enq.await_count == 1  # the first line: the Router
    task = await _thread_in_call(cid, "idle")
    with (
        patch.object(voice_phone.tasks_repo, "get_task", AsyncMock(return_value=task)),
        patch.object(voice_phone.tasks_repo, "put_trigger", AsyncMock(return_value=({}, True))) as put,
        patch.object(voice_phone.task_conversation, "gate_input", AsyncMock(return_value=None)),
        patch.object(voice_phone.admission, "_to_task", AsyncMock()) as to_task,
    ):
        out = client.post(url, json={"n": 1, "text": "The first time works."}, headers=W).json()
    assert enq.await_count == 1  # no new Router turn, so no new thread
    trig = put.await_args.args[1]
    assert trig["routedTo"] == {"taskId": T1} and trig["triggerId"] == out["triggerId"]
    assert trig["ref"]["origin"] == "phone_contact" and trig["ref"]["messageId"] == out["messageId"]
    to_task.assert_awaited_once_with("org_1", T1, out["triggerId"])
    assert out["taskId"] == T1  # the voice can correlate a follow-up to the original task's final line


async def test_a_callers_line_after_the_thread_closed_goes_to_the_router(client, router_seat):
    _, enq = router_seat
    call = await _start(client)
    task = await _thread_in_call(call["callId"], "failed")
    with patch.object(voice_phone.tasks_repo, "get_task", AsyncMock(return_value=task)):
        client.post(f"/api/v1/internal/voice/calls/{call['callId']}/turn", json={"n": 1, "text": "Hi?"}, headers=W)
    assert enq.await_count == 1


async def test_a_booking_under_way_is_said_only_once_the_call_has_one(client, router_seat):
    """8 Nov prod final smoke: "Vera is booking your founding team setup for Saturday, November 14 at 12:00 PM" was
    said while nothing was booked. Until the call's record (or the caller) has a booking, that sentence is replaced;
    the rest of the line, and lines about bookings that exist, are said as written."""
    call = await _start(client)
    cid, base = call["callId"], f"/api/v1/internal/voice/calls/{call['callId']}"
    lines = [
        "Vera is booking your setup for Saturday at 12:00 PM.",
        "Thanks, Taylor. You\u2019re all set for Saturday at 1 PM. Anything else?",
        "You're booked Tuesday at 3 PM. Which new time works?",  # a booking from an earlier call, being moved
        "That 2 PM slot is booked. We have 3:00 or 5:00 PM.",
        "We have 1:00, 3:00 or 5:00 PM. Which works? Should I book it?",
    ]
    for i, text in enumerate(lines):
        await _agent_line(cid, text, f"l{i}")
    said = [m["text"] for m in client.post(f"{base}/events", json={"after": 0}, headers=W).json()["messages"]]
    nb = voice_phone.NOT_BOOKED
    assert said == [nb, f"Thanks, Taylor. {nb} Anything else?", *lines[2:]]
    await voice_phone._patch_call(cid, lambda c: {"bookings": [{"start": "2026-11-14T13:00"}]})
    said = [m["text"] for m in client.post(f"{base}/events", json={"after": 0}, headers=W).json()["messages"]]
    assert said == lines


async def test_a_callers_upcoming_booking_keeps_the_line(client, router_seat):
    call = await _start(client)
    cid, base = call["callId"], f"/api/v1/internal/voice/calls/{call['callId']}"
    await _agent_line(cid, "Vera is booking the move to Sunday.", "l0")
    with patch.object(voice_phone, "_has_upcoming", AsyncMock(return_value=True)) as has:
        said = client.post(f"{base}/events", json={"after": 0}, headers=W).json()["messages"]
    assert said[0]["text"] == "Vera is booking the move to Sunday."
    has.assert_awaited_once_with("org_1", "+14155550133")


async def test_another_calls_caller_thread_is_never_picked(client, router_seat):
    _, enq = router_seat
    call = await _start(client)
    task = {**await _thread_in_call(call["callId"], "idle"), "audience": {"kind": "room", "roomId": "other-call"}}
    with (
        patch.object(voice_phone.tasks_repo, "get_task", AsyncMock(return_value=task)),
        patch.object(voice_phone.admission, "_to_task", AsyncMock()) as to_task,
    ):
        client.post(f"/api/v1/internal/voice/calls/{call['callId']}/turn", json={"n": 1, "text": "Yes."}, headers=W)
    assert enq.await_count == 1 and not to_task.called


async def test_a_paused_line_sends_no_caller_line_to_the_router(client, router_seat):
    _, enq = router_seat
    call = await _start(client, paused=True)
    r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/turn", json={"n": 0, "text": "hi"}, headers=W)
    assert r.status_code == 403 and not enq.called


async def test_call_events_are_lightbulbs_lines_only(client, router_seat):
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    client.post(f"{base}/turn", json={"n": 0, "text": "Open Sunday?"}, headers=W)
    with patch.object(voice_phone, "agent_lines", AsyncMock(return_value={"messages": [], "after": 1})) as lines:
        assert client.post(f"{base}/events", json={"after": 0}, headers=W).json() == {"messages": [], "after": 1}
    lines.assert_awaited_once_with(call["callId"], 0)
    real = client.post(f"{base}/events", json={"after": 0}, headers=W).json()
    assert real["messages"] == [] and real["after"] == 1  # the caller's own line is never said back


def test_card_numbers_and_the_expiry_or_cvv_after_them_are_redacted():
    r = voice_phone.redact_cards
    assert r("my card is 4111 1111 1111 1111 exp 12/27 cvv 123, thanks") == (
        "my card is [card number removed] [removed], thanks"
    )
    assert r("4111-1111-1111-1111") == "[card number removed]"
    assert r("4111111111111111 123") == "[card number removed] [removed]"  # a CVV said straight after
    assert r("card 5555555555554444, call me at 4155550133") == "card [card number removed], call me at 4155550133"
    for kept in (
        "4111 1111 1111 1112",  # not Luhn-valid
        "call me at +1 415 555 0133",  # a phone number: 11 digits
        "booking 2026-11-14T13:00 for 45 min",
        "provided card number digits 4111...",  # 8 Nov prod step: 4 digits are no card number
    ):
        assert r(kept) == kept, kept


async def test_no_card_digits_reach_the_steps_messages_transcript_or_the_call_room(client, router_seat):
    _, enq = router_seat
    call = await _start(client)
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    card = "4111 1111 1111 1111, expires 08/29"
    msg = {"text": f"Wants to pay the deposit with card {card}", "name": "Taylor"}
    assert client.post(f"{base}/message", json=msg, headers=W).status_code == 200
    assert client.post(f"{base}/turn", json={"n": 0, "text": f"Card is {card}"}, headers=W).status_code == 200
    tx = [{"role": "user", "text": f"The number is {card}"}]
    rec = client.post(f"{base}/end", json={"durationSec": 30, "transcript": tx}, headers=W).json()
    room_texts = [m["text"] for m in (await messaging_repo.list_messages(call["callId"], 0, 50))[0]]
    saved = f"{rec} {room_texts} {enq.call_args.args[1].ref}"
    assert "4111" not in saved and "08/29" not in saved
    assert "[card number removed]" in rec["steps"][0]["text"] and room_texts


async def test_a_placeholder_name_is_never_stored_as_the_caller(client):
    call = await _start(client, caller="+14155550177", room="phone-dev-_n_1")
    base = f"/api/v1/internal/voice/calls/{call['callId']}"
    for name in ("Unknown", "unknown caller", "N/A", "Caller", "Anonymous"):
        assert client.post(f"{base}/message", json={"text": "Call me back", "name": name}, headers=W).status_code == 200
    rec = client.post(f"{base}/end", json={"durationSec": 20}, headers=W).json()
    assert rec["callerName"] == "" and all(m["name"] == "" for m in rec["messages"])
    again = await _start(client, caller="+14155550177", room="phone-dev-_n_2")
    assert again["callerName"] == ""  # no "Unknown, a returning client"
    assert voice_phone.real_name(" Taylor  Test ") == "Taylor Test"


async def test_an_answer_after_hang_up_is_kept_on_the_record_home_reads(client):
    """8 Nov prod re-run #3, card-102109: the card answer landed 9 s after hang-up and only the room had it."""
    call = await _start(client)
    cid = call["callId"]
    late = {"messageId": "m5", "text": "There's no deposit to pay. You'll securely put a card on file at setup."}
    await voice_phone.after_call(cid, late)  # during the call: nothing (the voice says it)
    assert "afterCall" not in (await messaging_repo.get_room(cid))["call"]
    client.post(f"/api/v1/internal/voice/calls/{cid}/end", json={"durationSec": 56}, headers=W)
    await voice_phone.after_call(cid, {"messageId": "m6", "text": "x", "progress": True})  # progress: not an answer
    await voice_phone.after_call(cid, late)
    rec = (await messaging_repo.get_room(cid))["call"]
    assert rec["afterCall"] == [{"messageId": "m5", "text": late["text"]}]
    assert rec["steps"][-1]["text"].startswith("After the call, not heard by the caller: There's no deposit")


@pytest.mark.parametrize(
    "preferences,expected_voice,expected_spanish",
    [
        ({}, "marin", False),
        ({"voice": "Warm"}, "marin", False),
        ({"voice": "willow", "spanish": True}, "willow", True),
        ({"voice": "quartz", "spanish": False}, "quartz", False),
    ],
)
async def test_call_bootstrap_resolves_preferences_without_mutating_document(
    client, preferences, expected_voice, expected_spanish
):
    bootstrap = await _start(client, **preferences)
    assert bootstrap["voice"] == expected_voice
    assert bootstrap["spanish"] is expected_spanish
    assert await messaging_repo.get_voice_agent("org_1") == {**CONFIG, **preferences}


# --- security gate (Phase 3 review, 9 Nov): M1, M2, M4, M5 ---------------------------------------------------------


@pytest.mark.parametrize("withheld", ["anonymous", "restricted", "unknown", "+266696687", "+2562533", "", "12345"])
async def test_withheld_callers_share_no_identity(client, withheld):
    """M1: a withheld number is caller "": no memory from another withheld caller, no bookings to list, move or
    cancel; booking and messages still work, and the end writes no caller row."""
    first = await _start(client, caller=withheld, room="phone-w-1")
    assert (await messaging_repo.get_room(first["callId"]))["call"]["caller"] == ""
    base = f"/api/v1/internal/voice/calls/{first['callId']}"
    day = _tomorrow()
    booked = client.post(f"{base}/book", json={"start": f"{day}T11:00", "service": "gel", "name": "Ana"}, headers=W)
    assert booked.status_code == 200 and booked.json()["booked"] is True
    client.post(f"{base}/remember", json={"fact": "likes the window seat"}, headers=W)
    assert client.post(f"{base}/message", json={"text": "call me"}, headers=W).status_code == 200
    client.post(f"{base}/end", json={"durationSec": 20}, headers=W)
    assert await messaging_repo._get(messaging_repo.owner_pk("org_1"), "VOICECALLER#") is None
    assert await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICECALLER#{withheld}") is None

    other = await _start(client, caller="anonymous", room="phone-w-2")  # a different withheld caller
    assert other["callerName"] == "" and other["callerMemory"] == []
    obase = f"/api/v1/internal/voice/calls/{other['callId']}"
    assert client.post(f"{obase}/my-bookings", headers=W).json() == {"bookings": []}
    assert client.post(f"{obase}/change", json={"start": f"{day}T11:00"}, headers=W).status_code == 404


async def test_run_out_forward_and_transfer_never_dial_a_non_us_number(client):
    """M2: a hand-off saved before the US/CA check (or written around it) is neither forwarded to nor transferred."""
    with patch.object(voice_phone, "month_seconds", AsyncMock(return_value=1500 * 60)):
        call = await _start(client, runOut="forward", handoff={"number": "+18765550123", "onlyOpenHours": False})
    assert call["forwardTo"] is None
    out = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/transfer", headers=W).json()
    assert out == {"transferTo": None}


async def test_every_per_call_route_needs_that_calls_token(client):
    """M4: the worker secret alone opens nothing but start; one call's token never opens another call."""
    a = await _start(client, room="phone-t-1")
    b = await _start(client, room="phone-t-2")
    assert a["callToken"] and a["callToken"] != b["callToken"]
    base = f"/api/v1/internal/voice/calls/{a['callId']}"
    routes = {"turn": {"n": 0, "text": "hi"}, "book": {"start": f"{_tomorrow()}T11:00", "service": "g", "name": "A"},
              "change": {"start": f"{_tomorrow()}T11:00"}, "my-bookings": {}, "message": {"text": "x"},
              "transfer": {}, "events": {"after": 0}, "end": {"durationSec": 1}, "remember": {"fact": "x"},
              "availability": {"date": _tomorrow()}, "unknown": {"question": "x"},
              "start-work": {"title": "t", "brief": "b"}}  # fmt: skip
    for route, body in routes.items():
        for token in ("", "wrong", b["callToken"]):
            headers = {**W, voice_phone.CALL_TOKEN_HEADER: token}
            r = client.post(f"{base}/{route}", json=body, headers=headers)
            assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_call_token", (route, token)
    assert (await messaging_repo.get_room(a["callId"]))["call"]["status"] == "live"  # nothing was done
    replay = client.post("/api/v1/internal/voice/calls", headers=W,
                         json={"calledNumber": NUMBER, "callerNumber": "+14155550133", "livekitRoom": "phone-t-1"})  # fmt: skip
    assert replay.json()["callToken"] == a["callToken"]  # a replayed start is the same call


async def test_billed_duration_is_capped_by_the_time_since_start(client):
    """M4: a just-started call cannot bill a day; the cap is the server's time since startedAt plus END_GRACE_S."""
    call = await _start(client, ago=0)
    rec = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 86400}, headers=W)
    assert rec.json()["durationSec"] <= voice_phone.END_GRACE_S + 2
    later = await _start(client, room="phone-d-2", ago=100)
    rec = client.post(f"/api/v1/internal/voice/calls/{later['callId']}/end", json={"durationSec": 90}, headers=W)
    assert rec.json()["durationSec"] == 90  # an honest duration is kept


async def test_a_full_line_answers_with_messages_only(client, router_seat, monkeypatch):
    """M5: over LIVE_CALLS_PER_LINE at once, a call is answered but only takes messages; an end frees its slot."""
    monkeypatch.setattr(voice_phone, "LIVE_CALLS_PER_LINE", 2)
    calls = [await _start(client, caller=f"+1415555014{i}", room=f"phone-c-{i}") for i in range(3)]
    assert [c["mode"] for c in calls] == ["live", "live", "message_only"] and not any(calls[2]["can"].values())
    base = f"/api/v1/internal/voice/calls/{calls[2]['callId']}"
    assert client.post(f"{base}/turn", json={"n": 0, "text": "hi"}, headers=W).status_code == 403
    assert client.post(f"{base}/message", json={"text": "call me"}, headers=W).status_code == 200
    client.post(f"/api/v1/internal/voice/calls/{calls[0]['callId']}/end", json={"durationSec": 5}, headers=W)
    assert (await _start(client, caller="+14155550149", room="phone-c-9"))["mode"] == "live"


async def test_one_number_calling_all_day_is_limited_to_messages(client, router_seat, monkeypatch):
    """M5: CALLS_PER_CALLER_PER_DAY per number per workspace per day; withheld callers count as one number."""
    monkeypatch.setattr(voice_phone, "CALLS_PER_CALLER_PER_DAY", 2)
    modes = []
    for i in range(3):
        call = await _start(client, room=f"phone-n-{i}")
        modes.append(call["mode"])
        client.post(f"/api/v1/internal/voice/calls/{call['callId']}/end", json={"durationSec": 5}, headers=W)
    assert modes == ["live", "live", "message_only"]
    withheld = [(await _start(client, caller=c, room=f"phone-a-{c}"))["mode"] for c in ("anonymous", "", "unknown")]
    assert withheld == ["live", "live", "message_only"]


async def test_bookings_per_caller_per_day_are_capped(client, monkeypatch):
    """M5: BOOKINGS_PER_CALLER_PER_DAY; a refused or failed booking does not count; over it, messages only."""
    monkeypatch.setattr(voice_phone, "BOOKINGS_PER_CALLER_PER_DAY", 1)
    call = await _start(client)
    base, day = f"/api/v1/internal/voice/calls/{call['callId']}", _tomorrow()
    book = lambda t: client.post(f"{base}/book", json={"start": f"{day}T{t}", "service": "gel", "name": "A"}, headers=W)  # noqa: E731
    assert book("23:30").status_code == 400  # past close: nothing booked, nothing counted
    assert book("11:00").status_code == 200
    r = book("15:00")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert (await messaging_repo.get_room(call["callId"]))["call"]["limited"]
    assert client.post(f"{base}/message", json={"text": "call me"}, headers=W).status_code == 200


async def test_router_turns_per_call_are_capped(client, router_seat, monkeypatch):
    """M5: TURNS_PER_CALL distinct lines (a retried line is the same); then the worker takes a message."""
    monkeypatch.setattr(voice_phone, "TURNS_PER_CALL", 2)
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/turn"
    assert [client.post(url, json={"n": n, "text": f"line {n}"}, headers=W).status_code for n in (0, 1, 1)] == [200] * 3
    r = client.post(url, json={"n": 2, "text": "line 2"}, headers=W)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    assert client.post(url, json={"n": 0, "text": "line 0"}, headers=W).status_code == 403  # the call is limited now
    assert (await messaging_repo.get_room(call["callId"]))["call"]["limited"]


async def test_first_three_answered_calls_push_the_owner_and_the_fourth_does_not(client):
    with patch.object(voice_phone.push_service, "send_push_to_user", AsyncMock()) as push:
        for i in range(4):
            call = await _start(client, room=f"phone-dev-_x_{i}")
            url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
            client.post(url, json={"durationSec": 30}, headers=W)
            client.post(url, json={"durationSec": 30}, headers=W)  # a retried end never pushes twice
    assert push.await_count == 3
    assert push.await_args.args[0] == "user_owner" and push.await_args.args[3]["source"] == "voice_call"


async def test_a_call_that_needs_you_leaves_the_push_to_its_card(client):
    with patch.object(voice_phone.push_service, "send_push_to_user", AsyncMock()) as push:
        call = await _start(client)
        base = f"/api/v1/internal/voice/calls/{call['callId']}"
        client.post(f"{base}/unknown", json={"question": "Parking?"}, headers=W)
        client.post(f"{base}/end", json={"durationSec": 30}, headers=W)
    push.assert_not_awaited()


def test_call_card_push_carries_the_kind_and_a_first_name_only():
    from core.services import questions

    q = {
        "kind": "question",
        "subkind": "handoff",
        "title": "Maria wanted a person",
        "text": "Caller: +14155550133",
        "preview": {"firstName": "Maria"},
    }
    assert questions.card_copy(q, "Lightbulb")[1] == "Caller wanted a person: Maria"
    q["preview"] = {}
    assert questions.card_copy(q, "Lightbulb")[1] == "Caller wanted a person"


async def test_a_retried_end_counts_one_visit(client):
    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    for _ in range(2):
        assert client.post(url, json={"durationSec": 60}, headers=W).status_code == 200
    seen = await voice_phone._seen("org_1", "+14155550133")
    assert seen["visits"] == 1


async def test_the_sweep_ends_a_call_left_live_past_the_cap(client):
    import time

    from core.services import voice_calls

    call = await _start(client)
    await voice_calls.sweep_phone_calls(int(time.time()))  # fresh: left alone
    assert (await voice_phone._call(call["callId"]))[1]["status"] == "live"
    await voice_calls.sweep_phone_calls(int(time.time()) + voice_calls.PHONE_STALE_S + 1)
    ended = (await voice_phone._call(call["callId"]))[1]
    assert ended["status"] == "ended" and ended["durationSec"] == 0  # duration unknown: bills nothing
    assert "Closed by cleanup" in ended["summary"]
    await voice_calls.sweep_phone_calls(int(time.time()) + 10_000)  # index row gone: nothing to repeat


async def test_overlapping_starts_for_one_room_return_the_stored_token(client):
    """#5193 review: two starts race for one LiveKit room; the loser read no room, so it minted its own token. It
    must return the token the room kept, or its worker gets 401 on every later route."""
    first = await _start(client, room="phone-race", ago=0)
    real, missed = messaging_repo.get_room, []

    async def racing(room_id):
        if room_id == first["callId"] and not missed:
            missed.append(room_id)
            return None  # the loser's replay check ran before the winner's create landed
        return await real(room_id)

    with patch.object(voice_phone.messaging_repo, "get_room", racing):
        loser = client.post("/api/v1/internal/voice/calls", headers=W,
                            json={"calledNumber": NUMBER, "callerNumber": "+14155550133", "livekitRoom": "phone-race"})  # fmt: skip
    assert missed and loser.json()["callToken"] == first["callToken"]
    headers = {**W, voice_phone.CALL_TOKEN_HEADER: loser.json()["callToken"]}
    url = f"/api/v1/internal/voice/calls/{first['callId']}/events"
    assert client.post(url, json={"after": 0}, headers=headers).status_code == 200


async def test_two_interleaved_starts_for_one_room_both_return_the_stored_token(client):
    """#5193 review (Codex): two starts for one LiveKit room both find no room and both mint a candidate token
    before either create_room runs. The winner's create stores its token; the loser's create replays the stored
    META. Both must answer with the stored token, or the loser's worker gets 401 on every later route."""
    import asyncio

    real, arrived, both = messaging_repo.create_room, [], asyncio.Event()

    async def held_until_both_minted(**kw):
        arrived.append(kw["extra"]["callToken"])
        if len(arrived) == 2:
            both.set()
        await both.wait()  # neither create runs until both starts have their own candidate
        if kw["extra"]["callToken"] == arrived[1]:
            await asyncio.sleep(0.05)  # the second to arrive creates last: it is the loser
        return await real(**kw)

    await voice_phone.put_line(NUMBER, LINE)
    await messaging_repo.put_voice_agent("org_1", CONFIG, "user_owner")
    body = voice_phone.StartIn(calledNumber=NUMBER, callerNumber="+14155550133", livekitRoom="phone-interleaved")
    with patch.object(voice_phone.messaging_repo, "create_room", held_until_both_minted):
        a, b = await asyncio.gather(voice_phone.start_call(body), voice_phone.start_call(body))
    stored = (await messaging_repo.get_room(a["callId"]))["callToken"]
    assert len(set(arrived)) == 2  # two candidates were minted
    assert a["callToken"] == b["callToken"] == stored
    # ...and the one call is one unit of the caller's daily allowance (Codex, #5193 :357)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICECALLS#{day}#+14155550133")
    assert row["units"] == {a["callId"]}


async def test_a_new_booking_at_a_start_already_used_counts_again(client, monkeypatch):
    """#5193 review (Codex :1041): the allowance is keyed by booking id, not start. Book 10:00, move it away, book
    10:00 again: two reservations, two units, so a third new booking is over a cap of 2."""
    monkeypatch.setattr(voice_phone, "BOOKINGS_PER_CALLER_PER_DAY", 2)
    call = await _start(client)
    base, day = f"/api/v1/internal/voice/calls/{call['callId']}", _tomorrow()
    book = lambda t: client.post(f"{base}/book", json={"start": f"{day}T{t}", "service": "gel", "name": "A"}, headers=W)  # noqa: E731
    assert book("10:00").json()["booked"] is True
    moved = client.post(f"{base}/change", json={"start": f"{day}T10:00", "newStart": f"{day}T12:00"}, headers=W)
    assert moved.json()["moved"] is True  # a move is the same reservation: no unit
    assert book("10:00").json()["booked"] is True  # a new reservation at the freed start: the second unit
    r = book("15:00")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"


async def test_a_failed_slot_release_still_bills_and_a_retried_end_finishes_once(client):
    """#5193 review: releasing the live slot is best-effort (it expires); minutes, usage and caller memory always
    run, and a retried end does each once."""
    from core.billing import voice_billing

    call = await _start(client)
    url = f"/api/v1/internal/voice/calls/{call['callId']}/end"
    with patch.object(voice_phone.messaging_repo, "release_voice_slot", AsyncMock(side_effect=RuntimeError("ddb"))):
        assert client.post(url, json={"durationSec": 60}, headers=W).status_code == 200
    assert client.post(url, json={"durationSec": 60}, headers=W).status_code == 200
    assert await voice_billing.counted_seconds("org_1", None) == 60
    seen = await messaging_repo._get(messaging_repo.owner_pk("org_1"), "VOICECALLER#+14155550133")
    assert seen["visits"] == 1
    assert await messaging_repo._get(messaging_repo.owner_pk("org_1"), f"VOICEUSAGE#{call['callId']}")
