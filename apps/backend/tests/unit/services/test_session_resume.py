"""Resume (spec §5.7): real Moto transactions through the relay, the HTTP routes and the device route."""

import json
import time
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_resume
from core.services.local_session_grants import AgentInvocation
from core.services.messaging import ApiProblem
from tests.unit.services import (
    test_local_session_relay as relay_tests,
    test_local_sessions as registry_tests,
    test_session_pickup as pickup_tests,
)
from tests.unit.services.test_local_session_relay import connect, fixture, registration, relay
from tests.unit.services.test_local_sessions import ALICE, BOB, CLI, MAC, OWNER, ROOM

db = registry_tests.db
transport = relay_tests.transport

CAROL = AuthContext(user_id="user_carol", org_id=OWNER)
CONVERSATION = "11111111-2222-4333-8444-555555555555"
RESUME = "aaaaaaaa-1111-4111-8111-111111111111"
NAMES = {"user_alice": "Alice", "user_bob": "Bob"}


async def _name(user_id):
    return NAMES.get(user_id, user_id)


@pytest.fixture(autouse=True)
def _desktop_starts_allowed(monkeypatch):
    monkeypatch.setattr(session_resume, "submissions_enabled", lambda owner: True)
    monkeypatch.setattr("core.services.messaging.human_display_name", _name)


def descriptor(**changes):
    session = dict(
        provider="claude",
        nativeConversationId=CONVERSATION,
        providerVersion="2.1.288",
        status="idle",
        activeTurnId=None,
    )
    session.update(changes)
    return registration(**session)


def successor(previous="generation-1"):
    frame = descriptor(generation="generation-2")
    return {**frame, "previousGeneration": previous} if previous else frame


async def ended_session(transport, *senders, team_control=False) -> str:
    """Alice's Claude session, with ``senders`` let in, ended by her Mac."""
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    sid = (await relay(transport).handle_message("conn-1", descriptor()))["sessionId"]
    for person in senders:
        await messaging_repo.ensure_user_stream(OWNER, person.user_id)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET allowedSenders = :senders, teamControl = :team",
        ExpressionAttributeValues={":senders": [p.user_id for p in senders], ":team": team_control},
    )
    ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": int(time.time())}
    await relay(transport).handle_message("conn-1", ended)
    return sid


def ask(auth, sid, message=None, resume_id=RESUME):
    return session_resume.request_resume(auth, sid, session_resume.ResumeRequest(resumeId=resume_id, message=message))


async def want(sid):
    return await messaging_repo._get(repo.user_pk(OWNER, ALICE.user_id), f"RESUMEWANT#{MAC}#{sid}")


def commands(transport):
    return [body["command"] for _, body in transport[3] if body["type"] == "local.session.command"]


@pytest.mark.asyncio
async def test_the_end_keeps_who_could_send_as_who_may_resume_and_a_new_run_clears_it(transport):
    sid = await ended_session(transport, BOB)
    row = await repo.get_session(OWNER, sid)
    assert row["resumeAllowed"] == [BOB.user_id] and "allowedSenders" not in row
    assert (await service.get_session(CAROL, sid)).resume_allowed == [BOB.user_id]
    await relay(transport).handle_message("conn-1", successor())
    assert "resumeAllowed" not in await repo.get_session(OWNER, sid)
    assert (await service.get_session(ALICE, sid)).resume_allowed == []


@pytest.mark.asyncio
async def test_team_control_at_the_end_lets_nobody_resume(transport):
    sid = await ended_session(transport, team_control=True)
    assert "resumeAllowed" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_resume_is_for_the_owner_and_who_could_send_never_an_agent(transport):
    sid = await ended_session(transport, BOB)
    with pytest.raises(ApiProblem, match="Only the owner"):
        await ask(CAROL, sid)
    agent = AgentInvocation(OWNER, ROOM, "agent-1", int(time.time()) + 60, "invocation-1", ALICE.user_id)
    with pytest.raises(ApiProblem, match="Only a person"):
        await ask(agent, sid)
    resumed = await ask(BOB, sid, "carry on")
    assert resumed.resume.state == "pending" and resumed.resume.by_user_id == BOB.user_id
    assert "carry on" not in resumed.model_dump_json()
    assert "carry on" not in (await service.get_session(CAROL, sid)).model_dump_json()


@pytest.mark.asyncio
async def test_a_running_session_or_a_disabled_workspace_cannot_be_resumed(transport, monkeypatch):
    sid = await ended_session(transport, BOB)
    await relay(transport).handle_message("conn-1", successor())
    with pytest.raises(ApiProblem, match="still running"):
        await ask(ALICE, sid)
    monkeypatch.setattr(session_resume, "submissions_enabled", lambda owner: False)
    with pytest.raises(ApiProblem, match="not available"):
        await ask(ALICE, sid)


@pytest.mark.asyncio
async def test_one_resume_waits_per_session_and_a_retry_is_the_same_request(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    assert (await ask(BOB, sid, "carry on")).resume.by_user_id == BOB.user_id  # the same click again
    with pytest.raises(ApiProblem, match="already asked"):
        await ask(ALICE, sid, resume_id="bbbbbbbb-2222-4222-8222-222222222222")
    wanted = await want(sid)
    assert {k: wanted[k] for k in ("resumeId", "byUserId", "byName", "mode", "conversationId")} == {
        "resumeId": RESUME,
        "byUserId": BOB.user_id,
        "byName": "Bob",
        "mode": "plan",
        "conversationId": CONVERSATION,
    }
    assert wanted["expiresAt"] - wanted["at"] == 24 * 3600 and "text" not in wanted


def test_the_owner_resumes_in_the_mode_it_ran_in_capped_at_default():
    mode = session_resume._mode
    assert mode({"stats": {"permissionMode": "acceptEdits"}}, True) == "default"
    assert mode({"stats": {"permissionMode": "bypassPermissions"}}, True) == "default"
    assert mode({"stats": {"permissionMode": "plan"}}, True) == "plan"
    assert mode({}, True) == "default"
    assert mode({"stats": {"permissionMode": "default"}}, False) == "plan"


@pytest.mark.asyncio
async def test_a_used_command_id_cannot_become_a_resume(transport):
    sid = await ended_session(transport, BOB)
    messaging_repo._table().put_item(
        Item={"pk": repo.session_pk(OWNER, sid), "sk": f"COMMAND#{RESUME}", "requestId": RESUME}
    )
    with pytest.raises(ApiProblem, match="already used"):
        await ask(BOB, sid, "carry on")


@pytest.mark.asyncio
async def test_the_owner_takes_a_resume_back_and_a_waiting_one_goes_with_it(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    with pytest.raises(ApiProblem, match="Only the session's owner"):
        await session_resume.revoke_resume(BOB, sid, BOB.user_id)
    revoked = await session_resume.revoke_resume(ALICE, sid, BOB.user_id)
    assert revoked.resume_allowed == [] and revoked.resume is None
    assert await want(sid) is None
    with pytest.raises(ApiProblem, match="Only the owner"):
        await ask(BOB, sid)


@pytest.mark.asyncio
async def test_resume_routes(transport):
    from routers import messaging

    sid = await ended_session(transport, BOB)
    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda r, exc: JSONResponse(status_code=exc.status_code, content=exc.body()))
    base = f"/api/v1/messaging/sessions/{sid}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.dependency_overrides[get_current_user] = lambda: BOB
        assert (await client.post(f"{base}/resume", json={"resumeId": RESUME, "argv": ["sh"]})).status_code == 422
        resumed = await client.post(f"{base}/resume", json={"resumeId": RESUME, "message": "go on"})
        assert resumed.status_code == 200 and resumed.json()["resume"]["state"] == "pending"
        app.dependency_overrides[get_current_user] = lambda: ALICE
        revoked = await client.delete(f"{base}/resume-allowed/{BOB.user_id}")
        assert revoked.status_code == 200 and revoked.json()["resumeAllowed"] == []


@pytest.mark.asyncio
async def test_an_idle_mac_hears_about_the_resume_on_its_next_heartbeat(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    golden = fixture("device-heartbeat-ack-resume")
    # Nothing runs on the Mac: its heartbeat lists no sessions and still carries the want.
    ack = await relay(transport).handle_message("conn-1", {"type": "local.device.heartbeat", "sessions": []})
    (entry,) = ack["resume"]
    assert set(ack) == set(golden) and set(entry) == set(golden["resume"][0])
    assert entry == {
        "resumeId": RESUME,
        "sessionId": sid,
        "provider": "claude",
        "conversationId": CONVERSATION,
        "byUserId": BOB.user_id,
        "byName": "Bob",
        "title": "a session",
        "mode": "plan",
    }
    assert "carry on" not in json.dumps(ack)
    messaging_repo._table().update_item(
        Key={"pk": repo.user_pk(OWNER, ALICE.user_id), "sk": f"RESUMEWANT#{MAC}#{sid}"},
        UpdateExpression="SET expiresAt = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    later = await relay(transport).handle_message("conn-1", {"type": "local.device.heartbeat", "sessions": []})
    assert "resume" not in later  # TTL deletes lazily; an expired want is never delivered


@pytest.fixture
def mac_key(transport, db):
    key = Ed25519PrivateKey.generate()
    db[1].update_item(
        Key={"owner_id": OWNER},
        UpdateExpression="SET paired_devices.#mac.publicKey = :key",
        ExpressionAttributeNames={"#mac": MAC},
        ExpressionAttributeValues={":key": pickup_tests._public(key)},
    )
    return key


async def report(sid, body, key):
    from routers.desktop_session_resume import router

    app = FastAPI()
    app.include_router(router)
    headers = {"Authorization": f"Bearer {pickup_tests.token(MAC, OWNER, key)}"} if key else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/desktop/session-resume/{sid}/failed", json=body, headers=headers)


@pytest.mark.asyncio
async def test_the_mac_says_why_it_would_not_resume_and_is_not_asked_again(transport, mac_key):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid)
    assert (await report(sid, {"resumeId": RESUME, "code": "ALREADY_RUNNING"}, None)).status_code == 403
    assert (await report(sid, {"resumeId": RESUME, "code": "rm -rf"}, mac_key)).status_code == 400
    assert (await report(sid, {"resumeId": RESUME, "code": "ALREADY_RUNNING"}, mac_key)).status_code == 204
    status = (await service.get_session(BOB, sid)).resume
    assert (status.state, status.reason) == ("failed", "ALREADY_RUNNING")
    assert await want(sid) is None
    # A second report of the same request changes nothing; a refused resume may be asked again.
    assert (await report(sid, {"resumeId": RESUME, "code": "LAUNCH_FAILED"}, mac_key)).status_code == 204
    assert (await service.get_session(BOB, sid)).resume.reason == "ALREADY_RUNNING"
    again = await ask(BOB, sid, resume_id="cccccccc-3333-4333-8333-333333333333")
    assert again.resume.state == "pending"


@pytest.mark.asyncio
async def test_the_new_run_lets_the_resumer_in_and_sends_their_message_once(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    await relay(transport).handle_message("conn-1", successor())
    row = await repo.get_session(OWNER, sid)
    assert row["allowedSenders"] == [BOB.user_id] and "resume" not in row and await want(sid) is None
    (sent,) = commands(transport)
    assert (sent["requestId"], sent["text"], sent["expectedGeneration"], sent["actorUserId"]) == (
        RESUME,
        "carry on",
        "generation-2",
        BOB.user_id,
    )
    assert (await service.get_command(BOB, None, sid, RESUME)).state == "dispatched"
    # The relay reconnects and registers the same run again: nothing is sent twice.
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", descriptor(generation="generation-2"))
    assert len(commands(transport)) == 1
    # Bob's client sending the same request is recognised, never doubled.
    again = service.LocalSessionCommand(
        requestId=RESUME, expectedGeneration="generation-2", kind="send", text="carry on"
    )
    assert (await service.submit_command(BOB, None, sid, again)).request_id == RESUME
    assert len(commands(transport)) == 1


@pytest.mark.asyncio
async def test_a_lost_first_message_reads_unknown_and_is_never_sent_again(transport, monkeypatch):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)  # the dispatch lease is wall-clock
    gone = ClientError({"Error": {"Code": "GoneException", "Message": ""}}, "PostToConnection")
    with patch.object(transport[2]._client, "post_to_connection", side_effect=gone) as post:
        await relay(transport).handle_message("conn-1", successor())
        connect(transport, "conn-2", "2000")
        await relay(transport).handle_message("conn-2", descriptor(generation="generation-2"))
        assert post.call_count == 1
    assert (await service.get_command(BOB, None, sid, RESUME)).state == "unknown"


@pytest.mark.asyncio
async def test_a_session_ended_long_ago_resumes_without_its_old_generation(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid)
    # The Mac reaps an ended mirror a day after the end, so the new run names no predecessor.
    await relay(transport).handle_message("conn-1", successor(previous=None))
    row = await repo.get_session(OWNER, sid)
    assert row["generation"] == "generation-2" and row["allowedSenders"] == [BOB.user_id]
    assert commands(transport) == []  # no message, nothing sent


@pytest.mark.asyncio
async def test_a_running_session_still_fences_an_unchained_generation(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    await relay(transport).handle_message("conn-1", descriptor())
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", successor(previous=None))


@pytest.mark.asyncio
async def test_nobody_is_let_in_by_a_run_nobody_asked_for_or_by_someone_who_left(transport):
    sid = await ended_session(transport, BOB)
    await ask(BOB, sid, "carry on")
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    await relay(transport).handle_message("conn-1", successor())
    row = await repo.get_session(OWNER, sid)
    assert "allowedSenders" not in row and "resume" not in row and await want(sid) is None
    assert commands(transport) == []


@pytest.mark.asyncio
async def test_the_owner_resuming_lets_nobody_else_in(transport):
    sid = await ended_session(transport, BOB)
    await ask(ALICE, sid, "carry on")
    await relay(transport).handle_message("conn-1", successor())
    assert "allowedSenders" not in await repo.get_session(OWNER, sid)
    (sent,) = commands(transport)
    assert sent["actorUserId"] == ALICE.user_id
