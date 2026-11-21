"""Spark voice (/os/voice-session): the backend creates the GPT-Live WebRTC session with the company key and
returns OpenAI's SDP answer; a sideband per session meters it and can end it. OpenAI is always faked: the
create call through respx, the sideband through a fake socket."""

import asyncio
import json
import logging
from dataclasses import replace

import boto3
import httpx
import pytest
import websockets
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.voice import os_voice
from main import app
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401

A, B = Tenant("user_a", None), Tenant("user_b", None)
START = 1_800_000_000
LAN = "192.168.7.23"
OFFER = (
    "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "a=group:BUNDLE 0 1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    f"a=candidate:1 1 udp 2122260223 {LAN} 50123 typ host\r\n"
    "a=rtpmap:111 opus/48000/2\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=sctp-port:5000\r\na=sctp-init:AQAAHgpaMf8AAAAA\r\n"
)
ANSWER = "v=0\r\no=- 99 2 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\na=candidate:9 1 udp 1 203.0.113.9 3478 typ host\r\n"
SECRET_INSTRUCTIONS = "You are Spark. The person's private note: zebra-secret-91."
LIVE = "https://api.openai.com/v1/live/sessions"
USER = {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}


def body(**over):
    b = {
        "sdp": OFFER,
        "dcid": 4,
        "kind": "hold",
        "session": {
            "instructions": SECRET_INSTRUCTIONS,
            "input": [USER],
            "audio": {"output": {"voice": "marin"}},
            "delegation": {"type": "client", "acknowledgement_delay_ms": 800},
        },
        "client": {"appVersion": "1.0.0", "protocol": 2},
    }
    return b | over


def session(**over):
    return body(session=body()["session"] | over)


@pytest.fixture
def ddb(monkeypatch, jwks):  # noqa: F811
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=dynamodb.table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=dynamodb.table_name("containers"),
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield resource


@pytest.fixture(autouse=True)
async def no_leftover_sidebands():
    yield
    tasks = list(os_voice._TASKS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    os_voice._TASKS.clear()


@pytest.fixture
def users(ddb):
    return ddb.Table(dynamodb.table_name("users"))


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-not-real")


@pytest.fixture
def clock(monkeypatch):
    now = [START]
    monkeypatch.setattr("routers.os._now", lambda: now[0])
    monkeypatch.setattr("core.voice.os_voice.now", lambda: now[0])
    return now


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, **kw: seen.append((name, kw.get("dimensions"))))
    return seen


@pytest.fixture
def no_sideband(monkeypatch):
    started = []
    monkeypatch.setattr(os_voice, "start_sideband", started.append)
    return started


class FakeOpenAI:
    """POST /v1/live/sessions: each call gets a new session, unless `answer` is set (a response or an error)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answer: httpx.Response | Exception | None = None

    @property
    def called(self) -> bool:
        return bool(self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert (request.method, str(request.url).split("?")[0]) == ("POST", LIVE)
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        n = len(self.requests)
        return self.answer or httpx.Response(
            201, json={"session": {"id": f"live_{n}"}, "transport": {"type": "webrtc", "sdp": ANSWER}}
        )


@pytest.fixture
def openai(key, monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(os_voice, "_transport", httpx.MockTransport(fake))
    return fake


async def req(method, path, who: Tenant | None = A, json_body=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{path}", headers=who.headers if who else {}, json=json_body)


async def create(who=A, **over):
    return await req("POST", "/os/voice-session", who, body(**over))


def sent(fake) -> dict:
    return json.loads(fake.requests[-1].content)


@pytest.mark.parametrize("model,chosen", [(None, "gpt-live-1.1"), ("gpt-live-1", "gpt-live-1")])
async def test_create_returns_the_answer_and_sends_the_owned_config(ddb, clock, openai, no_sideband, model, chosen):
    r = await create(**({"model": model} if model else {}))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["sdp"] == ANSWER and out["expiresAt"] == START + os_voice.MAX_SESSION_S and out["sessionId"]
    req_ = openai.requests[-1]
    assert req_.headers["authorization"] == "Bearer sk-test-not-real"
    sent_body = sent(openai)
    assert sent_body["transport"] == {"type": "webrtc", "sdp": OFFER, "dcid": 4}
    assert "a=sctp-init:AQAAHgpaMf8AAAAA\r\n" in sent_body["transport"]["sdp"]
    assert req_.url.query == b""  # dcid never as a query parameter
    s = sent_body["session"]
    assert s["model"] == chosen and s["store"] is False
    assert s["client"] == {
        "data_channel": {
            "allowed_client_events": [
                "session.input_audio.mute",
                "session.input_audio.unmute",
                "session.instructions.append",
                "session.thinking.append",
                "session.commentary.append",
                "session.close",
            ]
        }
    }
    assert s["instructions"] == SECRET_INSTRUCTIONS and s["audio"] == {"output": {"voice": "marin"}}
    assert s["delegation"] == {"type": "client", "acknowledgement_delay_ms": 800}
    assert no_sideband == [out["sessionId"]]
    got = await req("GET", f"/os/voice-session/{out['sessionId']}")
    assert got.status_code == 200 and got.json() == {"status": "active", "endedReason": None}


async def test_a_model_off_the_list_is_refused(ddb, clock, no_sideband, openai):
    r = await create(model="gpt-realtime")
    assert (r.status_code, r.json()) == (400, {"detail": "model_not_allowed"})
    assert not openai.called


@pytest.mark.parametrize(
    "bad",
    [
        session(audio={"output": {"voice": "nova"}}),
        session(audio={"output": {"voice": "marin", "speed": 2}}),
        session(tools=[{"type": "function", "name": "x"}]),
        session(store=True),
        session(delegation={"type": "responses", "responses": {"model": "gpt-5"}}),
        session(delegation={"type": "client", "acknowledgement_delay_ms": 801}),
        session(instructions="x" * (65_536 + 1)),
        session(instructions="é" * 32_769),  # 65,538 UTF-8 bytes
        session(input=[USER] * 129),
        session(input="hi"),
        session(input=[{"role": "system", "content": [{"type": "input_text", "text": "x"}]}]),  # role
        session(input=[{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]),  # not text
        session(input=[{"role": "user", "content": [{"type": "input_text", "text": "x"}] * 2}]),  # two parts
        session(input=[{"role": "assistant", "content": [{"type": "input_text", "text": "x"}]}]),  # wrong part
        session(input=[{"role": "user", "content": [{"type": "input_text", "text": "x"}], "tools": []}]),
        session(input=[{"role": "user", "content": [{"type": "input_text", "text": "x" * 40_000}]}] * 2),  # total
        body(kind="shout"),
        body(dcid=5),
        body(session="x"),
    ],
)
async def test_invalid_config_never_reaches_openai(ddb, clock, no_sideband, openai, bad):
    r = await req("POST", "/os/voice-session", A, bad)
    assert (r.status_code, r.json()) == (400, {"detail": "invalid_config"})
    assert not openai.called


async def test_the_largest_allowed_config_passes(ddb, clock, no_sideband, openai):
    assistant = {"role": "assistant", "content": [{"type": "output_text", "text": "x" * 1_000}], "status": "completed"}
    big = [USER, assistant, {"role": "developer", "content": [{"text": "y" * 63_535}], "type": "message", "id": None}]
    r = await req("POST", "/os/voice-session", A, session(instructions="x" * 65_536, input=big))
    assert r.status_code == 201, r.text
    assert sent(openai)["session"]["input"] == big


@pytest.mark.parametrize("sdp", ["", "   ", "hello", 7, None, "v=0\r\n" + "a" * 70_000])
async def test_invalid_sdp(ddb, clock, no_sideband, openai, sdp):
    r = await create(sdp=sdp)
    assert (r.status_code, r.json()) == (400, {"detail": "invalid_sdp"})
    assert not openai.called


async def test_trial_ended_is_402_and_openai_is_not_called(ddb, clock, no_sideband, openai):
    assert (await req("GET", "/os/account")).status_code == 200
    clock[0] += 3 * 86400
    r = await create()
    assert (r.status_code, r.json()) == (402, {"detail": "trial_ended"})
    assert not openai.called


async def test_a_subscribed_person_passes_the_trial_check(ddb, clock, no_sideband, openai, users):
    from core.voice import os_voice

    assert (await req("GET", "/os/account")).status_code == 200
    users.update_item(
        Key={"user_id": "user_a"},
        UpdateExpression="SET os_subscription_status = :s",
        ExpressionAttributeValues={":s": "active"},
    )
    clock[0] += 3 * 86400
    assert (await create()).status_code == 201
    assert await os_voice._trial_ends_at("user_a") is None  # a live session is never closed as trial_ended


async def test_first_call_creates_the_account_then_proceeds(ddb, clock, no_sideband, openai, users):
    assert (await create()).status_code == 201
    row = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert row["os_trial_ends_at"] == START + 3 * 86400
    assert (await req("GET", "/os/account")).json()["status"] == "trial"


def active_rows(ddb) -> list[dict]:
    rows = ddb.Table(dynamodb.table_name("containers")).scan()["Items"]
    return [r for r in rows if r.get("status") == os_voice.ACTIVE]


async def test_a_new_session_replaces_the_open_one(ddb, clock, no_sideband, openai, users):
    first = (await create()).json()["sessionId"]
    second = await create()
    assert second.status_code == 201  # never too_many_sessions
    second = second.json()["sessionId"]
    assert users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_active"] == {second}
    assert rec(ddb, first)["closeRequested"] == "replaced" and "closeRequested" not in rec(ddb, second)
    assert (await create(B)).status_code == 201  # per person: B replaces nothing of A's
    assert "closeRequested" not in rec(ddb, second)


@pytest.mark.parametrize("n", [2, 3])
async def test_concurrent_opens_leave_exactly_one(ddb, clock, no_sideband, openai, users, n):
    rs = await asyncio.gather(*[create() for _ in range(n)])
    assert [r.status_code for r in rs] == [201] * n
    ids = {r.json()["sessionId"] for r in rs}
    keep = users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_active"]
    assert len(keep) == 1 and keep < ids
    assert {i for i in ids if not rec(ddb, i).get("closeRequested")} == keep  # every other one is told to close


@pytest.mark.parametrize(
    "answer,status,detail,retry",
    [
        (httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "rate_limit_exceeded"}}), 429, "capacity", 7),
        (httpx.Response(429, json={"error": {"code": "concurrency"}}), 429, "capacity", os_voice.RETRY_AFTER_S),
        (httpx.Response(500, json={"error": {}}), 502, "upstream", None),
        (httpx.Response(400, json={"error": {"code": "string_above_max_length", "message": "x"}}), 400, "invalid_config", None),
        (httpx.Response(422, json={"error": {"type": "invalid_request_error"}}), 400, "invalid_config", None),
        (httpx.Response(503, text="<html>"), 502, "upstream", None),
        (httpx.Response(201, json={"session": {}}), 502, "upstream", None),
        (httpx.ConnectError("refused"), 502, "upstream", None),
        (httpx.ReadTimeout("slow"), 504, "upstream", None),
    ],
)  # fmt: skip
async def test_openai_failures_map_and_free_the_slot(
    ddb, clock, no_sideband, openai, users, answer, status, detail, retry
):
    openai.answer = answer
    r = await create()
    assert r.status_code == status
    assert r.json() == {"detail": detail} | ({"retryAfterS": retry} if retry else {})
    assert "os_voice_active" not in users.get_item(Key={"user_id": "user_a"})["Item"]


async def test_a_failed_record_write_after_openai_answered_is_502(
    ddb, clock, no_sideband, openai, users, metrics, monkeypatch
):
    from botocore.exceptions import EndpointConnectionError

    real = os_voice.run_in_thread

    async def failing(fn, *args, **kwargs):
        if kwargs.get("UpdateExpression") == "SET liveId = :l":
            raise EndpointConnectionError(endpoint_url="x")
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(os_voice, "run_in_thread", failing)
    r = await create()
    assert (r.status_code, r.json()) == (502, {"detail": "upstream"})
    assert "os_voice_active" not in users.get_item(Key={"user_id": "user_a"})["Item"]
    assert metrics == [("os_voice.session.reaped_unconfirmed", None)]


async def test_key_not_configured_is_503(ddb, clock, no_sideband, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "lightbulb-placeholder")
    r = await create()
    assert (r.status_code, r.json()) == (503, {"detail": "voice_unavailable"})


async def test_only_the_opener_reads_a_session(ddb, clock, no_sideband, openai):
    sid = (await create()).json()["sessionId"]
    other = await req("GET", f"/os/voice-session/{sid}", B)
    unknown = await req("GET", f"/os/voice-session/vs_{'0' * 32}", B)
    malformed = await req("GET", "/os/voice-session/__call__", B)
    assert other.status_code == unknown.status_code == malformed.status_code == 404
    assert other.json() == unknown.json() == malformed.json()


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_voice_requires_a_human_session(ddb, clock, no_sideband, openai, token):
    assert (await create(replace(A, token=token))).status_code == 403
    assert (await req("POST", "/os/voice-session", None, body())).status_code in (401, 403)
    assert not openai.called


async def test_nothing_private_is_logged(ddb, clock, no_sideband, openai, caplog):
    caplog.set_level(logging.DEBUG)
    r = await create()
    assert r.status_code == 201
    openai.answer = httpx.Response(500, text=f"echo {OFFER} {SECRET_INSTRUCTIONS}")
    await create()
    openai.answer = httpx.Response(400, json={"error": {"code": "bad", "message": f"{OFFER} {SECRET_INSTRUCTIONS}"}})
    assert (await create()).status_code == 400
    bearer = A.headers["Authorization"].split()[1]
    for record in caplog.records:
        text = record.getMessage() + str(record.args) + str(record.exc_info)
        for secret in (LAN, "203.0.113.9", "zebra-secret-91", bearer, "sk-test-not-real", "sctp-init"):
            assert secret not in text, (secret, record.name)


# --- the sideband ------------------------------------------------------------------------------------


class FakeSocket:
    """One sideband connection: the test feeds server events; `sent` is what the backend sent."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        event = await self.inbox.get()
        if event is None:
            raise websockets.ConnectionClosedOK(None, None)
        return event if isinstance(event, str) else json.dumps(event)  # a str is a raw frame

    def push(self, *events):
        for e in events:
            self.inbox.put_nowait(e)


@pytest.fixture
def sockets(monkeypatch):
    opened: list[FakeSocket] = []

    def connect(url, **kwargs):
        assert kwargs["additional_headers"]["Authorization"] == "Bearer sk-test-not-real"
        ws = FakeSocket()
        ws.url = url
        opened.append(ws)
        return ws

    monkeypatch.setattr(os_voice, "_connect", connect)
    monkeypatch.setattr(os_voice, "TICK_S", 0.01)
    return opened


def rec(ddb, sid) -> dict:
    return ddb.Table(dynamodb.table_name("containers")).get_item(Key={"owner_id": f"__osvoice__#{sid}"}).get("Item")


def usage(s):
    return {"type": "session.usage.updated", "event_id": f"u{s}", "usage": {"seconds": s}}


def closed(s, reason="close_requested"):
    return {"type": "session.closed", "event_id": "c", "reason": reason, "usage": {"seconds": s}, "session": {}}


def idle() -> bool:
    return all(t.done() for t in os_voice._TASKS.values())


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def opened_session(who=A) -> str:
    r = await create(who)
    assert r.status_code == 201, r.text
    return r.json()["sessionId"]


async def replaced(sid):
    """The backend task that held the sideband goes away (a deploy): its socket task just stops."""
    task = os_voice._TASKS[sid]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_usage_is_recorded_and_the_close_counts_once(ddb, clock, openai, sockets, users):
    sid = await opened_session()  # the creating task attaches
    await until(lambda: sockets)
    assert sockets[0].url == "wss://api.openai.com/v1/live/sessions/live_1/attach"
    sockets[0].push(
        {"type": "session.started", "session": {"id": "live_1", "expires_at": START + 3600}}, usage(5), usage(12)
    )
    await until(lambda: rec(ddb, sid).get("usageSeconds") == 12)
    assert rec(ddb, sid)["expiresAt"] == START + 3600
    sockets[0].push(closed(15), closed(15))
    await until(lambda: rec(ddb, sid).get("endedReason"))
    row = rec(ddb, sid)
    assert row["endedReason"] == "client_closed" and row["usageSeconds"] == 15
    assert await os_voice.end_session(sid, "upstream", 99) is False  # a replayed close adds nothing
    person = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert person["os_voice_seconds"] == 15 and "os_voice_active" not in person


async def test_account_ending_mid_session_closes_it(ddb, clock, openai, sockets, users):
    sid = await opened_session()
    await until(lambda: sockets)
    clock[0] += 3 * 86400
    await until(lambda: {"type": "session.close"} in sockets[0].sent)
    sockets[0].push(closed(40))
    await until(lambda: rec(ddb, sid).get("endedReason"))
    assert rec(ddb, sid)["endedReason"] == "trial_ended"
    assert users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_seconds"] == 40


async def test_the_sweep_closes_a_session_past_the_hard_cap(ddb, clock, openai, sockets):
    sid = await opened_session()
    await until(lambda: sockets)
    await replaced(sid)
    clock[0] += os_voice.MAX_SESSION_S
    await os_voice.sweep_once()  # the lease lapsed: this task attaches, and the attach closes it
    await until(lambda: len(sockets) == 2 and {"type": "session.close"} in sockets[1].sent)
    assert rec(ddb, sid)["status"] == os_voice.ACTIVE  # the slot stays taken until OpenAI confirms
    sockets[1].push(closed(7200))
    await until(lambda: rec(ddb, sid).get("endedReason") == "expired")


async def test_a_lapsed_lease_is_reattached_and_not_double_counted(ddb, clock, openai, sockets, users, monkeypatch):
    sid = await opened_session()
    await until(lambda: sockets)
    sockets[0].push(usage(10))
    await until(lambda: rec(ddb, sid).get("usageSeconds") == 10)
    await replaced(sid)
    await os_voice.sweep_once()
    assert len(sockets) == 1  # the lease is still live: nobody else attaches yet
    clock[0] += os_voice.LEASE_S + 1
    monkeypatch.setattr(os_voice, "TASK_ID", "another-task")
    await os_voice.sweep_once()
    await until(lambda: len(sockets) == 2)
    sockets[1].push(usage(10), closed(20))  # the 3 s replay repeats the old usage
    await until(lambda: rec(ddb, sid).get("endedReason") == "client_closed")
    assert users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_seconds"] == 20


async def test_a_dropped_sideband_releases_its_lease_for_another_task(ddb, clock, openai, sockets):
    sid = await opened_session()
    await until(lambda: sockets)
    sockets[0].push(None)  # the socket closes without session.closed
    await until(idle)
    assert rec(ddb, sid)["status"] == os_voice.ACTIVE
    await os_voice.sweep_once()
    await until(lambda: len(sockets) == 2)
    sockets[1].push(closed(3))
    await until(lambda: rec(ddb, sid).get("endedReason") == "client_closed")


async def test_the_sweep_ends_a_session_that_never_got_an_openai_id(ddb, clock, key, users):
    await os_voice.reserve("user_a", "vs_dead", "hold", {}, START)
    clock[0] += os_voice.PENDING_S + 1
    await os_voice.sweep_once()
    assert rec(ddb, "vs_dead")["endedReason"] == "upstream"
    assert "os_voice_active" not in users.get_item(Key={"user_id": "user_a"})["Item"]


@pytest.mark.parametrize(
    "error,ended,dims",
    [
        (websockets.exceptions.InvalidStatus(httpx.Response(404)), "upstream", None),
        (websockets.exceptions.InvalidStatus(httpx.Response(500)), None, {"status": "5xx"}),
        (websockets.exceptions.InvalidStatus(httpx.Response(403)), None, {"status": "4xx"}),
        (OSError("unreachable"), None, {"status": "network"}),
    ],
)
async def test_an_attach_refusal(ddb, clock, openai, metrics, monkeypatch, error, ended, dims):
    def refuse(url, **kwargs):
        raise error

    monkeypatch.setattr(os_voice, "_connect", refuse)
    sid = await opened_session()
    await until(idle)
    if ended:  # OpenAI no longer has it: over, nothing left running
        assert rec(ddb, sid)["endedReason"] == ended
        assert metrics == []
        return
    # OpenAI may still have it: counted, loud, and the lease is free for the next sweep
    assert metrics == [("os_voice.sideband.attach_failed", dims)]
    assert rec(ddb, sid)["attachFailures"] == 1 and "leaseOwner" not in rec(ddb, sid)
    await os_voice.sweep_once()
    await until(idle)
    assert rec(ddb, sid)["attachFailures"] == 2
    attempts = []
    monkeypatch.setattr(os_voice, "_connect", lambda url, **kw: attempts.append(url) or refuse(url))
    clock[0] += os_voice.MAX_SESSION_S  # due: the sweep tries once to attach and close it, which fails
    await os_voice.sweep_once()
    await until(idle)
    assert len(attempts) == 1
    assert rec(ddb, sid)["endedReason"] == "upstream"
    assert metrics[-2:] == [("os_voice.sideband.attach_failed", dims), ("os_voice.session.reaped_unconfirmed", None)]


async def test_a_close_with_no_session_closed_is_reaped_unconfirmed(ddb, clock, openai, sockets, metrics, users):
    sid = await opened_session()
    await until(lambda: sockets)
    sockets[0].push(usage(30))
    clock[0] += 3 * 86400
    await until(lambda: {"type": "session.close"} in sockets[0].sent)
    clock[0] += os_voice.CLOSE_WAIT_S + 1  # OpenAI never answers the close
    await until(lambda: metrics)  # emitted after the row is ended
    assert rec(ddb, sid)["endedReason"] == "upstream"
    assert metrics == [("os_voice.session.reaped_unconfirmed", None)]
    assert users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_seconds"] == 30


async def test_an_unexpected_event_is_skipped_not_fatal(ddb, clock, openai, sockets, metrics, monkeypatch):
    sid = await opened_session()
    await until(lambda: sockets)
    sockets[0].push("{not json", "[1]", {"type": "session.usage.updated"})
    sockets[0].push({"type": "session.usage.updated", "usage": {"seconds": "9"}}, usage(9))
    await until(lambda: rec(ddb, sid).get("usageSeconds") == 9)
    assert metrics == [("os_voice.sideband.bad_event", None)] * 4
    assert not idle()  # still attached
    sockets[0].push({"type": "session.closed", "reason": "close_requested"})  # no usage: the last seen counts
    await until(lambda: rec(ddb, sid).get("endedReason") == "client_closed")
    assert rec(ddb, sid)["usageSeconds"] == 9


ITEM = {"role": "user", "content": [{"type": "input_text", "text": "x"}]}
JUNK = [[], {}, None, 1, True]


def _with(path: tuple, value) -> dict:
    """The happy-path body with one field (a key path; ints index lists) set to `value`."""
    b = json.loads(json.dumps(body() | {"model": "gpt-live-1.1"}))
    b["session"]["input"] = [json.loads(json.dumps(ITEM))]
    node = b
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return b


FIELDS = {
    ("sdp",): "invalid_sdp",
    ("model",): "model_not_allowed",
    ("kind",): "invalid_config",
    ("dcid",): "invalid_config",
    ("session",): "invalid_config",
    ("session", "instructions"): "invalid_config",
    ("session", "input"): "invalid_config",
    ("session", "audio"): "invalid_config",
    ("session", "audio", "output"): "invalid_config",
    ("session", "audio", "output", "voice"): "invalid_config",
    ("session", "delegation"): "invalid_config",
    ("session", "delegation", "type"): "invalid_config",
    ("session", "delegation", "acknowledgement_delay_ms"): "invalid_config",
    ("session", "input", 0): "invalid_config",
    ("session", "input", 0, "role"): "invalid_config",
    ("session", "input", 0, "type"): "invalid_config",
    ("session", "input", 0, "status"): "invalid_config",
    ("session", "input", 0, "id"): "invalid_config",
    ("session", "input", 0, "content"): "invalid_config",
    ("session", "input", 0, "content", 0): "invalid_config",
    ("session", "input", 0, "content", 0, "type"): "invalid_config",
    ("session", "input", 0, "content", 0, "text"): "invalid_config",
}
# Values the reference allows: an empty session or history; null instructions, item id and status; a 1 ms
# acknowledgement delay.
VALID = {
    (("session",), "{}"),
    (("session", "input"), "[]"),
    (("session", "instructions"), "None"),
    (("session", "input", 0, "status"), "None"),
    (("session", "input", 0, "id"), "None"),
    (("session", "delegation", "acknowledgement_delay_ms"), "1"),
}


@pytest.mark.parametrize("path,value", [(p, v) for p in FIELDS for v in JUNK], ids=repr)
async def test_any_value_in_any_field_is_a_contract_answer(ddb, clock, no_sideband, openai, path, value):
    r = await req("POST", "/os/voice-session", A, _with(path, value))
    if (path, repr(value)) in VALID:
        assert r.status_code == 201, r.text
    else:
        assert (r.status_code, r.json()) == (400, {"detail": FIELDS[path]})


@pytest.mark.parametrize(
    "role,part",
    [
        ("developer", {"type": "input_text", "text": "x"}),
        ("user", {"type": "input_text", "text": "x"}),
        ("assistant", {"type": "output_text", "text": "x"}),
        ("assistant", {"type": "text", "text": "x"}),
        ("assistant", {"text": "x"}),  # type omitted: the reference's default
    ],
)
async def test_each_role_with_the_part_types_the_reference_allows(ddb, clock, no_sideband, openai, role, part):
    r = await req("POST", "/os/voice-session", A, session(input=[{"role": role, "content": [part]}]))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    "role,part_type", [("developer", "output_text"), ("user", "text"), ("assistant", "input_text")]
)
async def test_a_part_type_the_role_does_not_allow_is_refused(ddb, clock, no_sideband, openai, role, part_type):
    r = await req(
        "POST", "/os/voice-session", A, session(input=[{"role": role, "content": [{"type": part_type, "text": "x"}]}])
    )
    assert (r.status_code, r.json()) == (400, {"detail": "invalid_config"})


@pytest.mark.parametrize(
    "delegation,ok",
    [
        ({"type": "client", "acknowledgement_delay_ms": 800}, True),
        ({"type": "client", "acknowledgement_delay_ms": 0}, True),
        ({"type": "client"}, True),
        ({"type": "client", "acknowledgement_delay_ms": 801}, False),
        ({"type": "client", "acknowledgement_delay_ms": -1}, False),
        ({"type": "client", "acknowledgement_delay_ms": "800"}, False),
        ({"type": "client", "acknowledgement_delay_ms": 800.0}, False),
        ({"type": "client", "acknowledgement_delay_ms": True}, False),
        ({"type": "client", "acknowledgement_delay_ms": 800, "x": 1}, False),
        ({"type": "decisions"}, False),
        ({}, False),
    ],
)
async def test_delegation(ddb, clock, no_sideband, openai, delegation, ok):
    r = await req("POST", "/os/voice-session", A, session(delegation=delegation))
    assert r.status_code == (201 if ok else 400), r.text
    if ok:
        assert sent(openai)["session"]["delegation"] == delegation


async def test_a_replaced_session_is_closed_by_its_sideband_and_counted_once(ddb, clock, openai, sockets, users):
    first = await opened_session()
    await until(lambda: sockets)
    sockets[0].push(usage(25))
    await until(lambda: rec(ddb, first).get("usageSeconds") == 25)
    second = await opened_session()
    await until(lambda: {"type": "session.close"} in sockets[0].sent)  # within a tick of being replaced
    await until(lambda: len(sockets) == 2)  # the new session's sideband attaches on its own schedule
    assert "session.close" not in str(sockets[1].sent)
    sockets[0].push(closed(27))
    await until(lambda: rec(ddb, first).get("endedReason"))
    got = await req("GET", f"/os/voice-session/{first}")
    assert got.json() == {"status": "ended", "endedReason": "replaced"}
    person = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert person["os_voice_seconds"] == 27 and person["os_voice_active"] == {second}
    assert await os_voice.end_session(first, "upstream", 99) is False
    assert [r["sessionId"] for r in active_rows(ddb)] == [second]


@pytest.mark.parametrize("attach", ["works", "fails"])
async def test_a_replaced_session_with_no_sideband_is_closed_by_the_sweep(
    ddb, clock, openai, sockets, metrics, monkeypatch, attach
):
    first = await opened_session()
    await until(lambda: sockets)
    await replaced(first)  # its task is gone
    clock[0] += os_voice.LEASE_S + 1
    if attach == "fails":
        monkeypatch.setattr(os_voice, "_connect", lambda url, **kw: (_ for _ in ()).throw(OSError("down")))
    await create()  # replaces it; its new sideband is the fake (or fails)
    await os_voice.sweep_once()  # picked up at once, not at the cap
    if attach == "works":
        await until(lambda: any({"type": "session.close"} in ws.sent for ws in sockets[1:]))
        ws = next(ws for ws in sockets[1:] if {"type": "session.close"} in ws.sent)
        assert ws.url.endswith("/live_1/attach")
        ws.push(closed(4))
        await until(lambda: rec(ddb, first).get("endedReason") == "replaced")
    else:
        await until(lambda: ("os_voice.session.reaped_unconfirmed", None) in metrics)
        assert rec(ddb, first)["endedReason"] == "upstream"


@pytest.mark.parametrize("trial", ["missing", "extended"])
async def test_a_trial_is_read_consistently_and_a_missing_one_never_closes(
    ddb, clock, openai, sockets, users, monkeypatch, trial
):
    reads = []
    real = os_voice.run_in_thread

    async def spy(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "get_item" and "user_id" in kwargs.get("Key", {}):
            reads.append(kwargs.get("ConsistentRead"))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(os_voice, "run_in_thread", spy)
    assert (await req("GET", "/os/account")).status_code == 200
    users.update_item(  # a trial that ends in a minute: the sideband caches this end
        Key={"user_id": "user_a"},
        UpdateExpression="SET os_trial_ends_at = :t",
        ExpressionAttributeValues={":t": START + 60},
    )
    sid = await opened_session()
    await until(lambda: sockets)
    if trial == "missing":
        users.update_item(Key={"user_id": "user_a"}, UpdateExpression="REMOVE os_trial_ends_at")
    else:  # a referral moved it past now, after the sideband cached the old end
        users.update_item(
            Key={"user_id": "user_a"},
            UpdateExpression="SET os_trial_ends_at = :t",
            ExpressionAttributeValues={":t": START + 30 * 86400},
        )
    clock[0] += 61  # past the cached end: the tick re-reads the row before closing
    for _ in range(20):
        await asyncio.sleep(os_voice.TICK_S)
    assert sockets[0].sent == [] and rec(ddb, sid)["status"] == os_voice.ACTIVE
    assert reads and all(r is True for r in reads)


# --- DELETE /os/voice-session/{id}: the app will not use it ------------------------------------------------


async def test_the_owner_closes_an_active_session(ddb, clock, openai, sockets, users):
    sid = await opened_session()
    await until(lambda: sockets)
    sockets[0].push(usage(3))
    await until(lambda: rec(ddb, sid).get("usageSeconds") == 3)
    r = await req("DELETE", f"/os/voice-session/{sid}")
    assert (r.status_code, r.content) == (204, b"")
    assert rec(ddb, sid)["closeRequested"] == "client_closed"
    await until(lambda: {"type": "session.close"} in sockets[0].sent)
    sockets[0].push(closed(4, reason="expired"), closed(4))
    await until(lambda: rec(ddb, sid).get("endedReason"))
    assert (await req("GET", f"/os/voice-session/{sid}")).json() == {"status": "ended", "endedReason": "client_closed"}
    person = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert person["os_voice_seconds"] == 4 and "os_voice_active" not in person
    again = await req("DELETE", f"/os/voice-session/{sid}")
    assert again.status_code == 204
    assert (
        rec(ddb, sid)["endedReason"] == "client_closed"
        and users.get_item(Key={"user_id": "user_a"})["Item"]["os_voice_seconds"] == 4
    )


async def test_a_second_delete_while_closing_changes_nothing(ddb, clock, openai, no_sideband):
    sid = await opened_session()
    assert (await req("DELETE", f"/os/voice-session/{sid}")).status_code == 204
    before = rec(ddb, sid)
    assert (await req("DELETE", f"/os/voice-session/{sid}")).status_code == 204
    assert rec(ddb, sid) == before


async def test_delete_never_overwrites_replaced(ddb, clock, openai, no_sideband):
    first = await opened_session()
    await opened_session()
    assert (await req("DELETE", f"/os/voice-session/{first}")).status_code == 204
    assert rec(ddb, first)["closeRequested"] == "replaced"


async def test_delete_of_anything_not_yours_is_the_gets_404(ddb, clock, openai, no_sideband):
    sid = await opened_session(A)
    gets = [await req("GET", f"/os/voice-session/{x}", B) for x in (sid, "vs_" + "0" * 32, "bad-id")]
    dels = [await req("DELETE", f"/os/voice-session/{x}", B) for x in (sid, "vs_" + "0" * 32, "bad-id")]
    assert [(r.status_code, r.json()) for r in dels] == [(g.status_code, g.json()) for g in gets]
    assert {r.status_code for r in dels} == {404}
    assert "closeRequested" not in rec(ddb, sid)


@pytest.mark.parametrize("token", ["oauth", "template", None])
async def test_delete_requires_a_human_session(ddb, clock, openai, no_sideband, token):
    sid = await opened_session()
    r = await req("DELETE", f"/os/voice-session/{sid}", replace(A, token=token) if token else None)
    assert r.status_code in (401, 403)
    assert "closeRequested" not in rec(ddb, sid)


async def test_a_closed_session_with_no_sideband_is_attached_and_closed_at_once(ddb, clock, openai, sockets):
    sid = await opened_session()
    await until(lambda: sockets)
    await replaced(sid)  # its task is gone
    clock[0] += os_voice.LEASE_S + 1
    assert (await req("DELETE", f"/os/voice-session/{sid}")).status_code == 204
    await until(lambda: len(sockets) == 2 and {"type": "session.close"} in sockets[1].sent)
    sockets[1].push(closed(2))
    await until(lambda: rec(ddb, sid).get("endedReason") == "client_closed")
