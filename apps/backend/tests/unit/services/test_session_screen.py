"""Watching a running terminal (spec §5.5): who may watch, what the Mac is told, and screen
frames that reach only current viewers and are stored nowhere."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import json as _json
import time

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import live_events, local_sessions as service, session_screen
from core.services.local_session_grants import AgentInvocation
from core.services.messaging import ApiProblem
from tests.unit.services.test_local_session_relay import (  # noqa: F401 -- fixtures
    _own_spawned_tasks,
    connect,
    db,
    fixture,
    heartbeat,
    relay,
    start,
    transport,
)
from tests.unit.services.test_local_sessions import ALICE, BOB, OUTSIDER, OWNER, ROOM


async def ack(transport, sid):
    return await relay(transport).handle_message("conn-1", heartbeat(sid))


@pytest.fixture(autouse=True)
async def _teammates_are_members(request):
    """Teammates are in the workspace (an active user stream) unless a test removes them."""
    if "transport" in request.fixturenames:
        request.getfixturevalue("transport")
        for person in (BOB, OUTSIDER):
            await messaging_repo.ensure_user_stream(OWNER, person.user_id)


@pytest.fixture(autouse=True)
def _nudges_reach_the_test_mac(request, monkeypatch):
    """`watch` nudges through the process relay; point it at this test's fake socket."""
    if "transport" in request.fixturenames:
        transport = request.getfixturevalue("transport")
        monkeypatch.setattr("core.services.local_session_relay.get_local_session_relay", lambda: relay(transport))


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


def frame(sid, **changes):
    return {**fixture("session-screen"), "sessionId": sid, "generation": "generation-1", **changes}


async def _published(transport):
    return [(h["channel"], _json.loads(h["events"][0])) for h in transport[4]]


@pytest.mark.asyncio
async def test_a_screen_frame_reaches_current_viewers_only_and_nothing_is_stored_or_answered(transport, db):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    before = await repo.get_session(OWNER, sid)
    transport[3].clear()
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    bob = await live_events.get_live_event_publisher().user_channel(OWNER, BOB.user_id)
    published = await _published(transport)
    assert [(c, e["type"]) for c, e in published] == [(bob, "workspace.session.screen")]
    assert published[0][1]["payload"] == {k: v for k, v in frame(sid).items() if k != "type"}
    assert await repo.get_session(OWNER, sid) == before, "screen traffic never writes META"
    assert transport[3] == [], "the Mac is never answered"
    rows = db[0].query(
        KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": repo.session_pk(OWNER, sid)}
    )["Items"]
    assert {r["sk"] for r in rows} == {"META", f"VIEWER#{BOB.user_id}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unshared", "generation", "connection", "expired", "invalid"])
async def test_a_screen_frame_is_dropped_silently(transport, monkeypatch, case):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    body, cid = frame(sid), "conn-1"
    if case == "unshared":
        await service.set_sharing(ALICE, sid, False)
    elif case == "generation":
        body = frame(sid, generation="generation-0")
    elif case == "connection":
        connect(transport, cid="conn-2", stamp="999")
        cid = "conn-2"
    elif case == "expired":
        later = time.time() + 46
        monkeypatch.setattr(session_screen, "_clock", lambda: later)
    else:
        body = frame(sid, data="!" * 20)
    transport[3].clear()
    transport[4].clear()
    await session_screen.forward(relay(transport), cid, body)
    assert [e["type"] for _, e in await _published(transport)] == []
    assert transport[3] == []


@pytest.mark.asyncio
async def test_screen_frames_over_the_minute_budget_are_dropped(transport, monkeypatch):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    monkeypatch.setattr(session_screen, "BUDGET_BYTES_PER_MINUTE", 4)
    monkeypatch.setattr(session_screen, "_spent", {})
    transport[4].clear()
    for seq in range(2):
        await session_screen.forward(relay(transport), "conn-1", frame(sid, seq=seq))  # 3 raw bytes each
    assert len(await _published(transport)) == 1
    reads = []
    real = repo.get_session
    monkeypatch.setattr(repo, "get_session", lambda *a: reads.append(a) or real(*a))
    await session_screen.forward(relay(transport), "conn-1", frame(sid, seq=2))
    assert reads == [] and len(await _published(transport)) == 1, "an over-budget frame costs no read"


@pytest.mark.asyncio
async def test_expired_watch_rows_never_push_a_live_session_out_of_the_ack(transport, db):
    sid = await start(transport)
    await session_screen.watch(BOB, sid)
    row = await repo.get_session(OWNER, sid)
    now = int(time.time())
    for i in range(session_screen.ACK_CAP + 1):  # TTL has not deleted them yet; all newer than ours
        db[0].put_item(
            Item={
                "pk": repo.user_pk(OWNER, row["humanUserId"]),
                "sk": f"WATCH#{row['deviceId']}#00000000-0000-4000-8000-{i:012d}",
                "sessionId": f"00000000-0000-4000-8000-{i:012d}",
                "since": now + 100 + i,
                "expiresAt": now - 1,
            }
        )
    assert [w["sessionId"] for w in (await ack(transport, sid))["watching"]] == [sid]


def test_the_golden_screen_frame_is_valid_and_fits_one_relay_frame():
    session_screen.Screen.model_validate(fixture("session-screen"))
    raw = "A" * (4 * ((session_screen.MAX_RAW_BYTES + 2) // 3))
    big = {**fixture("session-screen"), "generation": "g" * 64, "data": raw}
    assert len(_json.dumps(big, separators=(",", ":"))) <= 16 * 1024


def _nudges(transport):
    return [body for _, body in transport[3] if body["type"] == "local.session.watch"]


@pytest.mark.asyncio
async def test_a_new_viewer_nudges_a_capable_mac_once_and_a_repeat_post_does_not(transport):
    sid = await start(transport)
    await ack(transport, sid)  # the golden heartbeat advertises liveTerminal
    transport[3].clear()
    await session_screen.watch(BOB, sid)
    assert transport[3] == [("conn-1", {"type": "local.session.watch", "sessionId": sid, "generation": "generation-1"})]
    await session_screen.watch(BOB, sid)  # the same person, 15 s later: not new
    assert len(_nudges(transport)) == 1
    await session_screen.watch(ALICE, sid)  # another person is new
    assert len(_nudges(transport)) == 2


@pytest.mark.asyncio
async def test_a_mac_that_never_advertised_live_terminal_is_never_nudged(transport):
    sid = await start(transport)
    await session_screen.watch(BOB, sid)  # registered, no heartbeat yet
    older = heartbeat(sid)
    older.pop("liveTerminal")
    await relay(transport).handle_message("conn-1", older)
    await session_screen.watch(ALICE, sid)
    assert _nudges(transport) == []
    assert [w["sessionId"] for w in (await relay(transport).handle_message("conn-1", older))["watching"]] == [sid]


@pytest.mark.asyncio
async def test_a_reconnect_forgets_the_capability_until_the_next_heartbeat(transport):
    from tests.unit.services.test_local_session_relay import registration

    sid = await start(transport)
    await ack(transport, sid)
    connect(transport, cid="conn-2", stamp="2000")
    await relay(transport).handle_message("conn-2", registration())
    await session_screen.watch(BOB, sid)
    assert _nudges(transport) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unshared", "ended"])
async def test_no_nudge_for_an_unshared_or_ended_session(transport, db, case):
    sid = await start(transport)
    await ack(transport, sid)
    if case == "unshared":
        await service.set_sharing(ALICE, sid, False)
    else:
        db[0].update_item(
            Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
            UpdateExpression="SET endedAt = :t",
            ExpressionAttributeValues={":t": 1},
        )
    transport[3].clear()
    with pytest.raises(ApiProblem):
        await session_screen.watch(BOB, sid)
    assert transport[3] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["gone", "raises"])
async def test_a_failed_nudge_never_fails_viewing(transport, monkeypatch, failure):
    sid = await start(transport)
    await ack(transport, sid)

    async def send(_cid, _body):
        if failure == "raises":
            raise RuntimeError("network")
        return False

    monkeypatch.setattr(transport[2], "send_message_async", send)
    await session_screen.watch(BOB, sid)
    assert [w["sessionId"] for w in (await ack(transport, sid))["watching"]] == [sid]


def test_the_golden_watch_frame_fits_the_relay_envelope():
    from core.services.local_session_relay import check_envelope

    check_envelope(fixture("session-watch"))
    assert set(fixture("session-watch")) == {"type", "sessionId", "generation"}


@pytest.mark.asyncio
async def test_a_removed_member_cannot_keep_a_watch_alive(transport):
    sid = await start(transport)
    await session_screen.watch(BOB, sid)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    with pytest.raises(ApiProblem) as problem:
        await session_screen.watch(BOB, sid)  # their Clerk JWT is still valid
    assert problem.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    await session_screen.watch(ALICE, sid)  # the owner still watches
