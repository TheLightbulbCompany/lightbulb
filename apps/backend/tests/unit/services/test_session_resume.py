"""Resume (spec §5.7): real Moto transactions through the relay, the HTTP routes and the device route."""

import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_resume
from core.services.local_session_grants import AgentInvocation
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
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
