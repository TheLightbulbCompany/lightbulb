"""Watching a running terminal (spec §5.5): who may watch, what the Mac is told, and screen
frames that reach only current viewers and are stored nowhere."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import json as _json
import time

import pytest
from pydantic import ValidationError

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_screen
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
    published = await _published(transport)
    assert [(c, e["type"]) for c, e in published] == [(await _frames_channel(sid), "workspace.session.screen")]
    assert published[0][1]["payload"] == {k: v for k, v in frame(sid).items() if k != "type"}
    assert await repo.get_session(OWNER, sid) == before, "screen traffic never writes META"
    assert transport[3] == [], "the Mac is never answered"
    rows = db[0].query(
        KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": repo.session_pk(OWNER, sid)}
    )["Items"]
    assert {r["sk"] for r in rows} == {"META", f"VIEWER#{BOB.user_id}"}


async def _frames_channel(sid):
    return f"/ws/{await messaging_repo.ensure_workspace_id(OWNER)}/session-frames/{sid}"


@pytest.mark.asyncio
async def test_a_frame_is_one_publish_whatever_the_number_of_viewers(transport, monkeypatch):
    """Who hears it is AppSync's onSubscribe decision; the frame path reads no viewer's stream."""
    sid = await start(transport)
    for person in (ALICE, BOB, OUTSIDER):
        await session_screen.watch(person, sid)
    monkeypatch.setattr(session_screen, "_spent", {})
    monkeypatch.setattr(messaging_repo, "batch_get", None)
    monkeypatch.setattr(messaging_repo, "get_stream", None)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid, seq=0))
    assert [c for c, _ in await _published(transport)] == [await _frames_channel(sid)]


@pytest.mark.asyncio
async def test_the_frames_authorizer_marker_exists_exactly_while_the_session_is_shared(transport, db):
    """WORKSPACE#<ws>/SESSION#<sid> is what AppSync reads to admit a subscriber; it follows the row."""
    sid = await start(transport)
    key = {"pk": messaging_repo.workspace_pk(await messaging_repo.ensure_workspace_id(OWNER)), "sk": f"SESSION#{sid}"}
    assert db[0].get_item(Key=key)["Item"]["ownerId"] == OWNER
    await service.set_sharing(ALICE, sid, False)
    assert "Item" not in db[0].get_item(Key=key)
    await service.set_sharing(ALICE, sid, True)
    assert "Item" in db[0].get_item(Key=key)


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
async def test_screen_frames_forwarded_and_dropped_by_the_budget_are_metered(transport, monkeypatch):
    from unittest.mock import call, patch

    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    monkeypatch.setattr(session_screen, "BUDGET_BYTES_PER_MINUTE", 4)
    monkeypatch.setattr(session_screen, "_spent", {})
    with patch.object(session_screen, "put_metric") as metric:
        for seq in range(3):  # 3 raw bytes each: only the first fits the 4-byte minute
            await session_screen.forward(relay(transport), "conn-1", frame(sid, seq=seq))
    assert metric.call_args_list == [
        call("session.screen.frame.forwarded"),
        call("session.screen.bytes", 3, "Bytes"),
        call("session.screen.viewers", 1),
        call("session.screen.frame.dropped"),
        call("session.screen.frame.dropped"),
    ]


@pytest.mark.asyncio
async def test_a_failed_viewer_publish_is_not_counted_as_forwarded(transport, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, call, patch

    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    monkeypatch.setattr(session_screen, "_spent", {})
    publisher = MagicMock(publish_session_frames=AsyncMock(side_effect=RuntimeError("appsync down")))
    with (
        patch.object(session_screen.live_events, "get_live_event_publisher", return_value=publisher),
        patch.object(session_screen, "put_metric") as metric,
    ):
        await session_screen.forward(relay(transport), "conn-1", frame(sid, seq=0))
    publisher.publish_session_frames.assert_awaited_once()
    assert metric.call_args_list == [call("session.screen.publish.failed")]


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
async def test_a_new_viewer_nudges_a_capable_mac_once_and_a_repeat_post_does_not(transport, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)  # the golden heartbeat advertises liveTerminal
    transport[3].clear()
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    await session_screen.watch(BOB, sid)
    nudge = {"type": "local.session.watch", "sessionId": sid, "generation": "generation-1", "since": int(now)}
    assert transport[3] == [("conn-1", nudge)]
    # The ack that lists this viewer carries the nudge's own `since`: no news, no second history.
    assert (await ack(transport, sid))["watching"] == [{"sessionId": sid, "since": int(now)}]
    await session_screen.watch(BOB, sid)  # the same person, 15 s later: not new
    assert len(_nudges(transport)) == 1
    await session_screen.watch(ALICE, sid)  # another person is new
    assert len(_nudges(transport)) == 2


def _presence(transport):
    return [body for _, body in transport[3] if body["type"] == "local.session.presence"]


@pytest.mark.asyncio
async def test_a_new_viewer_is_named_to_the_owners_mac_at_once_not_at_the_next_ack(transport):
    """Prod 2026-10-07: the teammate had the picture in 0.5 s and the owner's band named them
    10-15 s later, at the next heartbeat ack."""
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", {**heartbeat(sid), "sessionPresence": True})
    transport[3].clear()
    await session_screen.watch(BOB, sid)
    # The picture first, then the name.
    assert [body["type"] for _, body in transport[3]] == ["local.session.watch", "local.session.presence"]
    assert [[v["userId"] for v in p["viewers"]] for p in _presence(transport)] == [[BOB.user_id]]
    await session_screen.watch(BOB, sid)  # a renewal is not news
    assert len(_presence(transport)) == 1
    await session_screen.watch(ALICE, sid, mounted=True)  # the owner is never listed: nothing to say
    assert len(_presence(transport)) == 1


@pytest.mark.asyncio
async def test_a_mac_without_the_band_gets_no_presence_push_for_a_new_viewer(transport):
    sid = await start(transport)
    await ack(transport, sid)  # the golden heartbeat: liveTerminal, no sessionPresence
    transport[3].clear()
    await session_screen.watch(BOB, sid)
    assert _presence(transport) == []


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
    assert set(fixture("session-watch")) == {"type", "sessionId", "generation", "since"}


@pytest.mark.asyncio
async def test_a_view_that_mounts_inside_its_lease_is_a_new_viewer_and_a_renewal_is_not(transport, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    await session_screen.watch(BOB, sid, mounted=True)
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 5)
    await session_screen.watch(BOB, sid)  # the 15 s renewal: nothing new
    assert [n["since"] for n in _nudges(transport)] == [int(now)]
    await session_screen.watch(BOB, sid, mounted=True)  # the page was refreshed: it has no screen
    assert [n["since"] for n in _nudges(transport)] == [int(now), int(now + 5)]
    assert (await ack(transport, sid))["watching"] == [{"sessionId": sid, "since": int(now + 5)}]


def test_the_viewing_route_passes_a_mount_on_and_takes_no_body_as_a_renewal(monkeypatch):
    import asyncio

    from routers import messaging as router

    seen = []

    async def watch(auth, session_id, mounted=False):
        seen.append(mounted)

    monkeypatch.setattr(router.session_screen, "watch", watch)
    asyncio.run(router.watch_local_session("s1", router.SessionViewingRequest(fresh=True), BOB))
    asyncio.run(router.watch_local_session("s1", router.SessionViewingRequest(), BOB))
    asyncio.run(router.watch_local_session("s1", None, BOB))  # a client older than `fresh`
    assert seen == [True, False, False]


@pytest.mark.asyncio
async def test_a_removed_member_cannot_keep_a_watch_alive(transport):
    sid = await start(transport)
    await session_screen.watch(BOB, sid)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    with pytest.raises(ApiProblem) as problem:
        await session_screen.watch(BOB, sid)  # their Clerk JWT is still valid
    assert problem.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    await session_screen.watch(ALICE, sid)  # the owner still watches


@pytest.mark.asyncio
async def test_a_session_without_a_live_terminal_is_neither_watched_nor_typed_into(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none"))
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(BOB, sid)
    assert refused.value.error.code == "LOCAL_SESSION_UNSUPPORTED"
    assert (await ack(transport, sid))["watching"] == [], "the Mac is never asked to stream it"
    with pytest.raises(ApiProblem) as typed:
        await session_screen.type_into(
            ALICE, sid, session_screen.SessionInput.model_validate({"data": "YQ==", "epoch": 1})
        )
    assert typed.value.error.code == "LOCAL_SESSION_UNSUPPORTED"
    assert not [b for _, b in transport[3] if b["type"] == "local.session.input"], "no keystroke reaches the Mac"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["history", "ping"])
async def test_history_and_ping_frames_reach_current_viewers_like_output(transport, kind):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    transport[4].clear()
    body = frame(sid, kind=kind, data="" if kind == "ping" else "G1tI")
    await session_screen.forward(relay(transport), "conn-1", body)
    published = await _published(transport)
    assert [(e["type"], e["payload"]["kind"]) for _, e in published] == [("workspace.session.screen", kind)]


def test_a_ping_carries_no_data_and_other_kinds_are_refused():
    golden = fixture("session-screen")
    session_screen.Screen.model_validate({**golden, "kind": "ping", "data": ""})
    for bad in ({"kind": "ping", "data": "G1tI"}, {"kind": "input"}):
        with pytest.raises(ValidationError):
            session_screen.Screen.model_validate({**golden, **bad})


@pytest.mark.asyncio
async def test_a_watched_session_that_registers_again_is_told_on_its_own_ack(transport, monkeypatch):
    """After a reconnect the Mac learned who watches only from its first heartbeat ack, after
    every session had registered again (25 s in prod, 2026-10-07): the viewer saw nothing."""
    from tests.unit.services.test_local_session_relay import registration

    sid = await start(transport)
    now = time.time()
    monkeypatch.setattr(session_screen, "_clock", lambda: now)
    assert "watch" not in await relay(transport).handle_message("conn-1", registration()), "nobody watches"
    await session_screen.watch(BOB, sid)
    connect(transport, cid="conn-2", stamp="2000")
    ack = await relay(transport).handle_message("conn-2", registration())
    # The nudge's own shape and the WATCH row's `since`: no second history burst at the next ack.
    assert ack["watch"] == {"sessionId": sid, "generation": "generation-1", "since": int(now)}
    monkeypatch.setattr(session_screen, "_clock", lambda: now + 61)
    assert "watch" not in await relay(transport).handle_message("conn-2", registration()), "the viewer left"


@pytest.mark.asyncio
async def test_a_session_admits_twenty_live_viewers_and_refuses_a_new_twenty_first(transport, db):
    sid = await start(transport)
    for n in range(session_screen.MAX_SESSION_VIEWERS - 1):  # 19 live viewers already
        db[0].put_item(
            Item={
                "pk": repo.session_pk(OWNER, sid),
                "sk": f"VIEWER#user_{n}",
                "userId": f"user_{n}",
                "expiresAt": int(time.time()) + 45,
            }
        )
    await session_screen.watch(BOB, sid)  # the 20th
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(OUTSIDER, sid)  # the 21st
    assert refused.value.error.code == "LOCAL_SESSION_VIEWERS_FULL"
    assert refused.value.error.message == "This session already has 20 live viewers."
    await session_screen.watch(BOB, sid)  # an existing viewer's heartbeat
    await session_screen.watch(BOB, sid, mounted=True)  # and its page refresh
    await session_screen.watch(ALICE, sid)  # the sharer
