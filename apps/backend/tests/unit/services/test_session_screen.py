"""Watching a running terminal (spec §5.5): who may watch, what the Mac is told, and screen
frames that reach only current viewers and are stored nowhere."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import time

import pytest

from core.repositories import local_sessions_repo as repo
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
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER, ROOM


async def ack(transport, sid):
    return await relay(transport).handle_message("conn-1", heartbeat(sid))


@pytest.mark.asyncio
async def test_a_teammate_watching_puts_the_session_in_its_macs_heartbeat_ack(transport):
    sid = await start(transport)
    assert (await ack(transport, sid))["watching"] == []
    await session_screen.watch(BOB, sid)
    assert [w["sessionId"] for w in (await ack(transport, sid))["watching"]] == [sid]


@pytest.mark.asyncio
async def test_since_moves_only_when_a_new_viewer_arrives_and_viewers_lapse_after_45_s(transport, monkeypatch):
    sid = await start(transport)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    await session_screen.watch(BOB, sid)
    first = (await ack(transport, sid))["watching"][0]["since"]
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 15)
    await session_screen.watch(BOB, sid)  # the same person, still watching
    assert (await ack(transport, sid))["watching"][0]["since"] == first
    await session_screen.watch(ALICE, sid)  # the owner, on the web
    assert (await ack(transport, sid))["watching"][0]["since"] == int(now + 15)
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 61)
    assert (await ack(transport, sid))["watching"] == []


@pytest.mark.asyncio
async def test_agents_never_watch_and_a_paused_session_is_not_streamed(transport):
    sid = await start(transport)
    agent = AgentInvocation(OWNER, ROOM, "friday", int(time.time()) + 60, "invocation-1", ALICE.user_id)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(agent, sid)
    assert refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    await session_screen.watch(BOB, sid)
    await service.set_sharing(ALICE, sid, False)
    for person, code in ((BOB, "LOCAL_SESSION_NOT_FOUND"), (ALICE, "LOCAL_SESSION_OFFLINE")):
        with pytest.raises(ApiProblem) as refused:
            await session_screen.watch(person, sid)
        assert refused.value.error.code == code
    assert (await ack(transport, sid))["watching"] == [], "the Mac stops at once"


@pytest.mark.asyncio
async def test_an_ended_session_cannot_be_watched(transport, db):
    sid = await start(transport)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET endedAt = :t",
        ExpressionAttributeValues={":t": 1},
    )
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(BOB, sid)
    assert refused.value.error.code == "LOCAL_SESSION_OFFLINE"
