"""Remote input (owner ruling 2026-10-03): admitted people type into a running session's live
terminal. Every call re-checks admission; refused calls send nothing; nothing typed is stored."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import base64
import json as _json
import time

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_screen
from core.services.local_session_grants import AgentInvocation
from core.services.messaging import ApiProblem
from tests.unit.services.test_local_session_relay import (  # noqa: F401 -- fixtures
    _own_spawned_tasks,
    db,
    fixture,
    heartbeat,
    relay,
    start,
    transport,
)
from tests.unit.services.test_local_sessions import ALICE, BOB, OUTSIDER, OWNER, ROOM

KEYS = base64.b64encode(b"ls -la\r").decode()


@pytest.fixture(autouse=True)
def _fresh(request, monkeypatch):
    monkeypatch.setattr(session_screen, "_typists", {})
    if "transport" in request.fixturenames:
        transport = request.getfixturevalue("transport")
        monkeypatch.setattr("core.services.local_session_relay.get_local_session_relay", lambda: relay(transport))


@pytest.fixture(autouse=True)
async def _teammates_are_members(request):
    """Teammates are in the workspace (an active user stream) unless a test removes them."""
    if "transport" in request.fixturenames:
        request.getfixturevalue("transport")
        for person in (BOB, OUTSIDER):
            await messaging_repo.ensure_user_stream(OWNER, person.user_id)


async def capable(transport, sid, remote_input=True):
    frame = heartbeat(sid)  # the golden heartbeat advertises remoteInput
    if not remote_input:
        frame.pop("remoteInput")
    await relay(transport).handle_message("conn-1", frame)


async def admit(sid, person=BOB):
    await service.request_join(person, sid)
    await service.decide_join(ALICE, sid, person.user_id, "allow")


def body(data=KEYS, epoch=3):
    return session_screen.SessionInput.model_validate({"data": data, "epoch": epoch})


def sent(transport):
    return [b for _, b in transport[3] if b["type"] == "local.session.input"]


def typed_rows(db, sid):
    return db[0].query(
        KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
        ExpressionAttributeValues={":pk": repo.session_pk(OWNER, sid), ":p": "TYPED#"},
    )["Items"]


def typing_events(transport):
    return [e for e in (_json.loads(h["events"][0]) for h in transport[4]) if e["type"] == "workspace.session.typing"]


async def refused(code, transport, auth, sid, b=None):
    with pytest.raises(ApiProblem) as problem:
        await session_screen.type_into(auth, sid, b or body())
    assert problem.value.error.code == code
    assert sent(transport) == [], "a refused call sends nothing to the Mac"


@pytest.mark.asyncio
async def test_an_admitted_member_is_forwarded_exactly_once_with_the_right_frame(transport, db):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    transport[3].clear()
    await session_screen.type_into(BOB, sid, body())
    assert transport[3] == [
        (
            "conn-1",
            {"type": "local.session.input", "sessionId": sid, "generation": "generation-1", "epoch": 3, "data": KEYS},
        )
    ]


@pytest.mark.asyncio
async def test_the_owner_is_always_admitted(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await session_screen.type_into(ALICE, sid, body())
    assert len(sent(transport)) == 1


@pytest.mark.asyncio
async def test_team_control_admits_any_member(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await service.set_team_control(ALICE, sid, True)
    transport[3].clear()
    await session_screen.type_into(OUTSIDER, sid, body())
    assert len(sent(transport)) == 1


@pytest.mark.asyncio
async def test_a_reader_who_is_not_admitted_is_refused(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await session_screen.watch(BOB, sid)  # may watch
    transport[3].clear()
    await refused("LOCAL_SESSION_FORBIDDEN", transport, BOB, sid)


@pytest.mark.asyncio
async def test_agents_are_refused_even_for_their_own_requester(transport):
    sid = await start(transport)
    await capable(transport, sid)
    transport[3].clear()
    agent = AgentInvocation(OWNER, ROOM, "friday", int(time.time()) + 60, "invocation-1", ALICE.user_id)
    await refused("LOCAL_SESSION_FORBIDDEN", transport, agent, sid)


def test_the_route_takes_a_clerk_session_and_no_room():
    """MCP's opaque OAuth tokens and device tokens never pass `get_current_user` (a JWT check);
    the route has no room parameter, so there is no room-scoped door."""
    import inspect

    from core.auth import get_current_user
    from routers import messaging as routes

    params = inspect.signature(routes.type_into_local_session).parameters
    assert set(params) == {"session_id", "body", "auth"}
    assert params["auth"].default.dependency is get_current_user


@pytest.mark.asyncio
async def test_removal_from_allowed_senders_refuses_the_very_next_call(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    await session_screen.type_into(BOB, sid, body())
    await service.remove_sender(ALICE, sid, BOB.user_id)
    transport[3].clear()
    await refused("LOCAL_SESSION_FORBIDDEN", transport, BOB, sid)


@pytest.mark.asyncio
async def test_pausing_sharing_refuses_input(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    await service.set_sharing(ALICE, sid, False)
    transport[3].clear()
    await refused("LOCAL_SESSION_NOT_FOUND", transport, BOB, sid)
    await refused("LOCAL_SESSION_OFFLINE", transport, ALICE, sid)


@pytest.mark.asyncio
async def test_an_ended_session_refuses_input(transport, db):
    sid = await start(transport)
    await capable(transport, sid)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET endedAt = :t",
        ExpressionAttributeValues={":t": 1},
    )
    transport[3].clear()
    await refused("LOCAL_SESSION_OFFLINE", transport, ALICE, sid)


@pytest.mark.asyncio
async def test_a_mac_that_never_advertised_remote_input_is_refused_without_sending(transport):
    sid = await start(transport)
    await capable(transport, sid, remote_input=False)
    await admit(sid)
    transport[3].clear()
    await refused("LOCAL_SESSION_UNSUPPORTED", transport, BOB, sid)


@pytest.mark.asyncio
async def test_over_the_rate_bound_is_429(transport, monkeypatch):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    for _ in range(session_screen.INPUT_BURST_CALLS):
        await session_screen.type_into(BOB, sid, body())
    transport[3].clear()
    await refused("RATE_LIMITED", transport, BOB, sid)
    await session_screen.type_into(ALICE, sid, body())  # per (session, person)


@pytest.mark.asyncio
async def test_over_the_byte_bound_is_429(transport, monkeypatch):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    monkeypatch.setattr(session_screen, "INPUT_BYTES_PER_MINUTE", 10)
    await session_screen.type_into(BOB, sid, body())  # 7 bytes
    transport[3].clear()
    await refused("RATE_LIMITED", transport, BOB, sid)


def test_input_over_4_kib_or_not_base64_is_invalid_and_the_golden_frame_fits():
    from pydantic import ValidationError

    from core.services.local_session_relay import check_envelope

    for data in (base64.b64encode(b"x" * 4097).decode(), "!!!!", ""):
        with pytest.raises(ValidationError):
            body(data=data)
    body(data=base64.b64encode(b"x" * 4096).decode())
    golden = fixture("session-input")
    check_envelope(golden)
    assert set(golden) == {"type", "sessionId", "generation", "epoch", "data"}
    biggest = {**golden, "generation": "g" * 256, "data": base64.b64encode(b"x" * 4096).decode()}
    check_envelope(biggest)


@pytest.mark.asyncio
async def test_one_audit_row_per_burst_with_byte_counts_and_no_content(transport, db, monkeypatch):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    await session_screen.type_into(BOB, sid, body())
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 9)
    await session_screen.type_into(BOB, sid, body())
    rows = typed_rows(db, sid)
    assert len(rows) == 1
    assert rows[0]["bytes"] == 14 and rows[0]["userId"] == BOB.user_id
    assert rows[0]["startedAt"] == int(now) and rows[0]["lastAt"] == int(now + 9)
    assert set(rows[0]) == {"pk", "sk", "userId", "startedAt", "lastAt", "bytes", "expiresAt"}
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 20)
    await session_screen.type_into(BOB, sid, body())
    assert len(typed_rows(db, sid)) == 2


@pytest.mark.asyncio
async def test_the_owner_hears_is_typing_once_per_burst(transport, monkeypatch):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    transport[4].clear()
    await session_screen.type_into(BOB, sid, body())
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 1)
    await session_screen.type_into(BOB, sid, body())
    events = typing_events(transport)
    assert [e["payload"] for e in events] == [{"sessionId": sid, "userId": BOB.user_id}]
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 6)  # still typing: a refresh
    await session_screen.type_into(BOB, sid, body())
    assert len(typing_events(transport)) == 2


@pytest.mark.asyncio
async def test_a_failed_send_is_offline_and_still_audited(transport, db, monkeypatch):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)

    async def gone(_cid, _body):
        return False

    monkeypatch.setattr(transport[2], "send_message_async", gone)
    with pytest.raises(ApiProblem) as problem:
        await session_screen.type_into(BOB, sid, body())
    assert problem.value.error.code == "LOCAL_SESSION_OFFLINE"
    assert len(typed_rows(db, sid)) == 1


@pytest.mark.asyncio
async def test_a_removed_member_cannot_type_even_while_still_admitted(transport):
    sid = await start(transport)
    await capable(transport, sid)
    await admit(sid)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    transport[3].clear()
    await refused("LOCAL_SESSION_FORBIDDEN", transport, BOB, sid)  # their Clerk JWT is still valid
    await session_screen.type_into(ALICE, sid, body())
    assert len(sent(transport)) == 1, "the owner still types"
