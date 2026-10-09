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
async def test_a_renewal_recreates_a_missing_marker_and_does_not_nudge(transport, db):
    """A session shared before put_session wrote markers, watched by a view whose lease is live."""
    sid = await start(transport)
    await session_screen.watch(BOB, sid)
    key = {"pk": messaging_repo.workspace_pk(await messaging_repo.ensure_workspace_id(OWNER)), "sk": f"SESSION#{sid}"}
    db[0].delete_item(Key=key)
    nudged = len(_nudges(transport))
    await session_screen.watch(BOB, sid)  # a renewal, not a fresh watch
    assert "Item" in db[0].get_item(Key=key)
    assert len(_nudges(transport)) == nudged


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
        return {"watching": 2, "viaLink": 3}

    monkeypatch.setattr(router.session_screen, "watch", watch)
    counts = asyncio.run(router.watch_local_session("s1", router.SessionViewingRequest(fresh=True), BOB))
    assert counts.model_dump() == {"watching": 2, "viaLink": 3}, "counts and nothing else"
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


# --- An "Anyone with the link" link's live terminal (session_links.viewing) ----------------------


async def _trade(token, lease):
    """A viewer about to reconnect with a lease AppSync already took (the authorizer marked it)."""
    import hashlib

    from core.services import session_links

    public, _, key = lease.partition(".")
    messaging_repo._table().update_item(
        Key={"pk": f"PUBWATCH#{public}", "sk": f"VIEWER#{hashlib.sha256(key.encode()).hexdigest()}"},
        UpdateExpression="SET connectedAt = :t",
        ExpressionAttributeValues={":t": int(time.time())},
    )
    return await session_links.viewing(token, lease, False, retake=True)


async def _public_link(sid):
    """An "Anyone" link and its token."""
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    return token, (await repo.get_session(OWNER, sid))["shareLink"]["publicId"]


@pytest.mark.asyncio
async def test_a_link_viewer_gets_frames_on_the_public_channel_with_no_session_or_workspace_id(transport, db):
    from core.services import session_links

    sid = await start(transport)
    await ack(transport, sid)  # the golden heartbeat advertises liveTerminal
    token, public = await _public_link(sid)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == [], "public, but nobody watches: no publish"
    transport[3].clear()
    seat = await session_links.viewing(token, None, True)
    assert seat.channel == f"/pub/{public}" and seat.lease.startswith(public + ".") and seat.watch_id == public
    assert [w["sessionId"] for w in (await ack(transport, sid))["watching"]] == [sid], "the Mac streams it"
    assert [m["type"] for _, m in transport[3]][0] == "local.session.watch", "a fresh viewer gets history now"
    stored = db[0].query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": f"PUBWATCH#{public}"})
    assert len(stored["Items"]) == 2 and seat.lease.split(".", 1)[1] not in str(stored["Items"]), "the key is hashed"
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    ((channel, event),) = await _published(transport)  # one publish: no workspace viewer, so no members' channel
    assert channel == f"/pub/{public}"
    assert event["payload"] == {
        **{k: v for k, v in frame(sid).items() if k != "type"},
        "sessionId": public,
        "generation": public,
    }
    assert sid not in _json.dumps(event) and "generation-1" not in _json.dumps(event)
    # A workspace viewer too: the members' channel is untouched, and each audience gets its own publish.
    await session_screen.watch(BOB, sid)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert sorted(c for c, _ in await _published(transport)) == sorted([await _frames_channel(sid), f"/pub/{public}"])


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["workspace", "only_me"])
async def test_narrowing_the_link_stops_public_frames_at_once_and_refuses_the_next_renewal(transport, revoke):
    from core.services import session_links

    sid = await start(transport)
    token, public = await _public_link(sid)
    seat = await session_links.viewing(token, None, True)
    if revoke == "workspace":
        await service.put_link(ALICE, sid, "workspace")
    else:
        await service.set_sharing(ALICE, sid, False)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert [c for c, _ in await _published(transport) if c.startswith("/pub/")] == []
    with pytest.raises(ApiProblem) as refused:
        await session_links.viewing(token, seat.lease, False)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    if revoke == "workspace":
        # Public again is a new channel: the old lease is for nothing, and a new one is not for the old channel.
        await service.put_link(ALICE, sid, "anyone")
        again = await session_links.viewing(token, seat.lease, False)
        assert again.channel != seat.channel and again.lease != seat.lease


@pytest.mark.asyncio
async def test_link_viewers_are_capped_and_a_renewal_keeps_its_seat(transport, monkeypatch):
    from core.services import session_links

    monkeypatch.setattr(session_links, "MAX_LINK_VIEWERS", 2)
    sid = await start(transport)
    token, _ = await _public_link(sid)
    first = await session_links.viewing(token, None, False)
    await session_links.viewing(token, None, False)
    with pytest.raises(ApiProblem) as full:
        await session_links.viewing(token, None, False)
    assert full.value.error.code == "LOCAL_SESSION_VIEWERS_FULL"
    assert (await session_links.viewing(token, first.lease, False)).lease == first.lease
    with pytest.raises(ApiProblem):  # a made-up key is no seat: it counts as a new viewer
        await session_links.viewing(token, first.lease + "x", False)


@pytest.mark.asyncio
async def test_a_workspace_link_or_an_ended_session_has_no_public_terminal(transport, db):
    from core.services import session_links

    sid = await start(transport)
    token = (await service.put_link(ALICE, sid, "workspace")).url.split("#", 1)[1]
    with pytest.raises(ApiProblem) as refused:
        await session_links.viewing(token, None, True)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    await service.put_link(ALICE, sid, "anyone")
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET endedAt = :t",
        ExpressionAttributeValues={":t": int(time.time())},
    )
    with pytest.raises(ApiProblem) as ended:
        await session_links.viewing(token, None, True)
    assert ended.value.error.code == "LOCAL_SESSION_OFFLINE"


@pytest.mark.asyncio
async def test_ending_stops_public_frames_and_the_next_renewal_though_the_mac_keeps_the_final_screen(transport, db):
    from core.services import session_links

    sid = await start(transport)
    token, _ = await _public_link(sid)
    seat = await session_links.viewing(token, None, True)
    await session_screen.watch(BOB, sid)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET endedAt = :t, screenUntil = :u, screenConnectionId = :c, terminal = :h",
        ExpressionAttributeValues={":t": int(time.time()), ":u": int(time.time()) + 30, ":c": "conn-1", ":h": "held"},
    )
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    # The kept final screen still reaches the workspace, and only the workspace.
    assert [c for c, _ in await _published(transport)] == [await _frames_channel(sid)]
    with pytest.raises(ApiProblem) as refused:
        await session_links.viewing(token, seat.lease, False)
    assert refused.value.error.code == "LOCAL_SESSION_OFFLINE"


@pytest.mark.asyncio
async def test_a_link_holder_cannot_make_the_mac_resend_its_history_in_a_loop(transport, monkeypatch):
    from core.services import session_links

    sid = await start(transport)
    await ack(transport, sid)  # liveTerminal
    token, _ = await _public_link(sid)
    now = time.time()
    monkeypatch.setattr(session_links.time, "time", lambda: now)
    seat = await session_links.viewing(token, None, False)
    transport[3].clear()
    for lease in (seat.lease, seat.lease, None):  # the same seat again, and a new seat: no more asks
        await session_links.viewing(token, lease, True)
    assert len(_nudges(transport)) == 1, "`fresh` is a request: one ask per link per window"
    monkeypatch.setattr(session_links.time, "time", lambda: now + session_links.ASK_EVERY_S)
    await session_links.viewing(token, seat.lease, True)
    assert len(_nudges(transport)) == 2


@pytest.mark.asyncio
async def test_lapsed_seats_are_dropped_when_a_new_one_is_taken(transport, db):
    from core.services import session_links

    sid = await start(transport)
    token, public = await _public_link(sid)
    for _ in range(3):
        await session_links.viewing(token, None, False)

    def seats():
        rows = db[0].query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": f"PUBWATCH#{public}"})
        return [r for r in rows["Items"] if r["sk"].startswith("VIEWER#")]

    for row in seats():  # their leases lapsed; DynamoDB's own TTL delete lags up to 48 h
        db[0].update_item(
            Key={"pk": row["pk"], "sk": row["sk"]},
            UpdateExpression="SET expiresAt = :t",
            ExpressionAttributeValues={":t": 1},
        )
    await session_links.viewing(token, None, False)
    assert len(seats()) == 1


@pytest.mark.asyncio
async def test_a_failed_read_of_the_link_watchers_never_costs_the_workspace_its_frame(transport, monkeypatch):
    from core.services import session_links

    sid = await start(transport)
    token, public = await _public_link(sid)
    await session_links.viewing(token, None, False)
    await session_screen.watch(BOB, sid)
    get = messaging_repo._get

    async def broken(pk, sk):
        if pk.startswith("PUBWATCH#"):
            raise RuntimeError("dynamo down")
        return await get(pk, sk)

    monkeypatch.setattr(messaging_repo, "_get", broken)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert [c for c, _ in await _published(transport)] == [await _frames_channel(sid)]


@pytest.mark.asyncio
async def test_a_reconnecting_viewer_trades_its_used_seat_for_a_new_one(transport, db, monkeypatch):
    """The authorizer lets a lease subscribe once, so a reconnect needs a new one; the old row
    goes, so one viewer never holds two seats."""
    from core.services import session_links

    sid = await start(transport)
    token, public = await _public_link(sid)
    first = await session_links.viewing(token, None, False)
    # About to connect with a lease AppSync never took (the connect did not reach it): renewed, no new seat.
    assert (await session_links.viewing(token, first.lease, False, retake=True)).lease == first.lease
    again = await _trade(token, first.lease)
    assert again.lease != first.lease and again.channel == first.channel
    rows = db[0].query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": f"PUBWATCH#{public}"})
    assert len([r for r in rows["Items"] if r["sk"].startswith("VIEWER#")]) == 1
    monkeypatch.setattr(session_links, "MAX_LINK_VIEWERS", 1)
    with pytest.raises(ApiProblem):  # the seat given up is gone: at a full session it is no way back in
        await session_links.viewing(token, first.lease, False)


@pytest.mark.asyncio
async def test_a_link_gives_out_a_bounded_number_of_seats_per_ten_minutes(transport, monkeypatch):
    from core.services import session_links

    monkeypatch.setattr(session_links, "MINTS_PER_WINDOW", 3)
    sid = await start(transport)
    token, _ = await _public_link(sid)
    now = time.time()
    monkeypatch.setattr(session_links.time, "time", lambda: now)
    seat = await session_links.viewing(token, None, False)
    for _ in range(2):  # every new subscription costs one, a traded seat included
        seat = await _trade(token, seat.lease)
    with pytest.raises(ApiProblem) as busy:
        await _trade(token, seat.lease)
    assert busy.value.error.code == "LOCAL_SESSION_VIEWERS_FULL"
    with pytest.raises(ApiProblem):
        await session_links.viewing(token, None, False)
    # The next window starts over. (The session's own liveness is read on this clock too.)
    monkeypatch.setattr(session_links.time, "time", lambda: now + 1)
    monkeypatch.setattr(session_links, "MINT_WINDOW_S", 1)
    assert (await session_links.viewing(token, None, False)).lease


@pytest.mark.asyncio
async def test_a_channel_gives_out_a_life_total_of_seats_and_then_the_link_moves_to_a_new_one(transport, monkeypatch):
    """An open subscription never ends by itself, so a channel's subscriptions are bounded by
    moving the link to a new channel: the old one's sockets all go silent."""
    from core.services import session_links

    monkeypatch.setattr(session_links, "MAX_CHANNEL_SEATS", 3)
    sid = await start(transport)
    token, public = await _public_link(sid)
    old = await session_links.viewing(token, None, False)
    for _ in range(2):
        await session_links.viewing(token, None, False)
    before = await repo.get_session(OWNER, sid)
    link = before["shareLink"]
    third = await session_links.viewing(token, None, False)  # past the total: seated on a new channel
    # A seat that raced the third past the total read the old row too. Its move is conditional on
    # the old publicId, so it moves nothing, and it is seated on the same new channel.
    await session_links._new_channel(before, public)
    fourth = await session_links.viewing(token, None, False)
    moved = (await repo.get_session(OWNER, sid))["shareLink"]
    assert moved["publicId"] != public and third.channel == fourth.channel == f"/pub/{moved['publicId']}"
    assert {k: v for k, v in moved.items() if k != "publicId"} == {k: v for k, v in link.items() if k != "publicId"}
    assert (await service.get_link(ALICE, sid)).audience == "anyone", "the same link, still public"
    # Frames go to the new channel only: every socket on the old one hears nothing more.
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert [c for c, _ in await _published(transport)] == [third.channel]
    # A viewer from the old channel follows at its next renewal: its lease renews nothing, and
    # it is seated on the new channel, for one seat.
    followed = await session_links.viewing(token, old.lease, False)
    assert followed.channel == third.channel and followed.lease != old.lease
    assert (await session_links.viewing(token, followed.lease, False)).lease == followed.lease


@pytest.mark.asyncio
async def test_a_refused_seat_costs_the_channel_nothing_and_the_budget_outlives_a_channel(transport, db, monkeypatch):
    """Codex on #3946: refused seats still counted against the channel, so a holder at the budget
    could spam requests, use the channel up and move it every few seconds, each move with a fresh
    budget. The budget is the link's and is counted first."""
    from core.services import session_links

    monkeypatch.setattr(session_links, "MAX_CHANNEL_SEATS", 2)
    monkeypatch.setattr(session_links, "MINTS_PER_WINDOW", 4)
    sid = await start(transport)
    token, first = await _public_link(sid)
    now = time.time()
    monkeypatch.setattr(session_links.time, "time", lambda: now)

    async def channel():
        return (await repo.get_session(OWNER, sid))["shareLink"]["publicId"]

    def taken(public):
        return int(db[0].get_item(Key={"pk": f"PUBWATCH#{public}", "sk": "WATCHED"})["Item"].get("seats", 0))

    seat = await session_links.viewing(token, None, False)
    for _ in range(3):  # the third moves the link; the window count goes with it
        seat = await _trade(token, seat.lease)
    second = await channel()
    assert second != first and taken(first) == 2 and taken(second) == 2
    for _ in range(5):  # at the budget: refused, and nothing moves or is counted
        with pytest.raises(ApiProblem) as busy:
            await _trade(token, seat.lease)
        assert busy.value.error.code == "LOCAL_SESSION_VIEWERS_FULL"
    assert await channel() == second and taken(second) == 2


@pytest.mark.asyncio
async def test_a_channels_seat_count_is_kept_for_as_long_as_a_socket_on_it_can_live(transport, db):
    """Codex on #3946: the count's row lapsed an hour after the last viewer, and the next seat
    started the same channel at one while sockets from before (AppSync keeps one for 24 h) still
    listened. The row now outlives them."""
    from core.services import session_links

    sid = await start(transport)
    token, public = await _public_link(sid)
    await session_links.viewing(token, None, False)
    row = db[0].get_item(Key={"pk": f"PUBWATCH#{public}", "sk": "WATCHED"})["Item"]
    assert int(row["seats"]) == 1
    assert int(row["expiresAt"]) - int(time.time()) > 24 * 3600  # `expiresAt` is the table's TTL attribute
    assert int(row["until"]) - int(time.time()) <= session_screen.VIEWER_TTL_S  # liveness is still 45 s


@pytest.mark.asyncio
async def test_the_owners_end_stops_public_frames_before_the_exit_output_and_a_refused_end_frees_them(transport, db):
    """Round 18: a link viewer read the provider's `claude --resume <id>` lines between End and the end."""
    import uuid

    from core.services import session_links
    from core.services.local_sessions import LocalSessionCommand

    sid = await start(transport)
    await ack(transport, sid)
    token, public = await _public_link(sid)
    await session_links.viewing(token, None, True)
    await session_screen.watch(BOB, sid)
    key = {"pk": repo.session_pk(OWNER, sid), "sk": "META"}
    db[0].update_item(Key=key, UpdateExpression="SET terminal = :h", ExpressionAttributeValues={":h": "held"})
    end = LocalSessionCommand(request_id=str(uuid.uuid4()), expected_generation="generation-1", kind="end")
    await service.submit_command(ALICE, None, sid, end)
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert [c for c, _ in await _published(transport)] == [await _frames_channel(sid)], "the workspace only"
    # The End never took effect (refused, or it never reached the Mac): the link gets frames again.
    db[0].update_item(
        Key=key,
        UpdateExpression="SET endAskedAt = :t",
        ExpressionAttributeValues={":t": int(time.time()) - session_screen.END_QUIET_S - 1},
    )
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert f"/pub/{public}" in [c for c, _ in await _published(transport)]


@pytest.mark.asyncio
async def test_link_viewers_are_counted_for_the_workspace_live_seats_of_the_current_channel_only(transport, db):
    """Owner ask 2026-10-08: the band and the web view say how many watch by link. A count, never who."""
    from core.services import session_links

    sid = await start(transport)
    await relay(transport).handle_message("conn-1", {**heartbeat(sid), "sessionPresence": True})
    token, public = await _public_link(sid)
    transport[3].clear()
    seat = await session_links.viewing(token, None, False)
    await session_links.viewing(token, seat.lease, False)  # a renewal is not news
    (pushed,) = _presence(transport)  # the new seat reached the band at once
    assert pushed["linkViewers"] == 1 and pushed["viewers"] == []
    assert seat.lease.split(".", 1)[1] not in _json.dumps(pushed) and public not in _json.dumps(pushed)
    await session_links.viewing(token, None, False)
    lapsed = {"pk": f"PUBWATCH#{public}", "sk": "VIEWER#gone", "expiresAt": int(time.time()) - 1}
    db[0].put_item(Item=lapsed)
    assert await session_screen.watch(BOB, sid) == {"watching": 1, "viaLink": 2}
    # Narrowed: zero at once, in the push the audience change already sends. Public again is a new channel.
    transport[3].clear()
    await service.put_link(ALICE, sid, "workspace")
    assert [p["linkViewers"] for p in _presence(transport)] == [0]
    await service.put_link(ALICE, sid, "anyone")
    assert (await session_screen.watch(BOB, sid))["viaLink"] == 0


@pytest.mark.asyncio
async def test_an_only_me_session_admits_its_owners_end_and_nothing_else(transport, db):
    """Round 18: the server admitted the owner's send and left the refusal to the Mac."""
    import uuid

    from core.services.local_sessions import LocalSessionCommand

    sid = await start(transport)
    await ack(transport, sid)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET terminal = :h",
        ExpressionAttributeValues={":h": "held"},
    )
    await service.set_sharing(ALICE, sid, False)

    def command(kind, **more):
        return LocalSessionCommand(request_id=str(uuid.uuid4()), expected_generation="generation-1", kind=kind, **more)

    with pytest.raises(ApiProblem) as refused:
        await service.submit_command(ALICE, None, sid, command("send", text="hello"))
    assert refused.value.error.code == "LOCAL_SESSION_CONFLICT"
    assert (await service.submit_command(ALICE, None, sid, command("end"))).state == "pending"
