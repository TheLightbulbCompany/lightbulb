"""Ended screens and terminal reasons (spec 2026-10-07 §5.1, §5.4): what the Mac reports and
what every reader is shown."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import time

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import (
    live_events,
    local_session_relay as relay_module,
    local_sessions as service,
    session_screen,
)
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
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER
from tests.unit.services.test_session_screen import (  # noqa: F401 -- autouse fixtures
    _nudges_reach_the_test_mac,
    _published,
    _teammates_are_members,
    ack,
    frame,
)


async def end(transport, sid, **extra):
    body = {
        **fixture("session-ended"),
        "sessionId": sid,
        "generation": "generation-1",
        "endedAt": int(time.time()),
        **extra,
    }
    return await relay(transport).handle_message("conn-1", body)


def screens(*sids):
    return {**fixture("device-heartbeat-screens"), "screens": list(sids)}


async def public(sid, viewer=BOB):
    return service._session_public(await repo.get_session(OWNER, sid), viewer.user_id)


def set_meta(db, sid, **fields):
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in fields),
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


def test_the_relay_lists_both_features_and_screens_is_no_device_feature():
    assert {"endedScreens", "terminalReason"} <= set(relay_module.FEATURES)
    frame = relay_module.DeviceHeartbeat.model_validate(fixture("device-heartbeat-screens"))
    assert frame.device_features() == []
    assert relay_module.DeviceHeartbeat.model_validate(fixture("device-heartbeat")).screens == []


@pytest.mark.asyncio
async def test_the_mac_names_why_a_running_session_has_no_terminal(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none", terminalReason="no-daemon"))
    seen = await public(sid)
    assert (seen.terminal, seen.terminal_reason) == (None, "no-daemon")
    assert seen.model_dump(by_alias=True, mode="json")["terminalReason"] == "no-daemon"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="held"))
    assert "terminalReason" not in await repo.get_session(OWNER, sid)
    assert (await public(sid)).terminal_reason is None
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none", terminalReason="vscode"))


@pytest.mark.asyncio
async def test_an_ended_held_session_keeps_its_terminal_while_the_mac_keeps_its_screen(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    seen = await public(sid)
    assert (seen.status, seen.terminal, seen.screen_available) == ("ended", "held", True)
    assert seen.terminal_reason is None
    set_meta(db, sid, screenUntil=1)
    seen = await public(sid)
    assert (seen.terminal, seen.screen_available) == (None, None)
    assert "screenAvailable" not in seen.model_dump(by_alias=True, mode="json", exclude_none=True)
    await relay(transport).handle_message("conn-1", screens(sid))
    row = await repo.get_session(OWNER, sid)
    assert row["screenConnectionId"] == "conn-1" and int(row["screenUntil"]) > time.time()
    assert (await public(sid)).screen_available is True


@pytest.mark.asyncio
async def test_an_end_without_a_kept_screen_shows_no_terminal(transport):
    sid = await start(transport)
    await end(transport, sid)
    seen = await public(sid)
    assert (seen.status, seen.terminal, seen.screen_available) == ("ended", None, None)


@pytest.mark.asyncio
async def test_screens_are_kept_only_for_this_macs_ended_held_sessions(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", screens(sid))
    assert "screenUntil" not in await repo.get_session(OWNER, sid), "a running session is not an ended screen"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="attach"))
    await end(transport, sid, screen=True)
    await relay(transport).handle_message("conn-1", screens(sid))
    assert "screenUntil" not in await repo.get_session(OWNER, sid), "an attach session's screen closes with it"


@pytest.mark.asyncio
async def test_a_kept_final_screen_is_watched_streamed_and_never_typed_into(transport):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    await session_screen.watch(BOB, sid)
    acked = await relay(transport).handle_message("conn-1", screens(sid))
    assert [w["sessionId"] for w in acked["watching"]] == [sid]
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    bob = await live_events.get_live_event_publisher().user_channel(OWNER, BOB.user_id)
    assert [(c, e["type"]) for c, e in await _published(transport)] == [(bob, "workspace.session.screen")]
    with pytest.raises(ApiProblem) as typed:
        await session_screen.type_into(
            ALICE, sid, session_screen.SessionInput.model_validate({"data": "YQ==", "epoch": 1})
        )
    assert typed.value.error.code == "LOCAL_SESSION_OFFLINE"


@pytest.mark.asyncio
async def test_a_screen_frame_for_an_ended_session_needs_its_kept_screen_and_connection(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    await session_screen.watch(BOB, sid)
    set_meta(db, sid, screenConnectionId="conn-2")
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == [], "another connection's frame"
    set_meta(db, sid, screenConnectionId="conn-1", screenUntil=1)
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == [], "a lapsed screen"


@pytest.mark.asyncio
async def test_a_lapsed_final_screen_refuses_watching_and_tells_the_open_panels(transport, db, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    set_meta(db, sid, screenUntil=1)
    told = []

    async def hint(owner, human, **_):
        told.append((owner, human))

    monkeypatch.setattr(relay_module, "publish_resources_changed", hint)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(BOB, sid)
    assert refused.value.error.code == "LOCAL_SESSION_OFFLINE"
    assert told == [(OWNER, ALICE.user_id)]
    with pytest.raises(ApiProblem):
        await session_screen.watch(BOB, sid)
    assert len(told) == 1, "a panel that pings again is not a reason to tell the workspace again"
    assert (await relay(transport).handle_message("conn-1", screens()))["watching"] == []


@pytest.mark.asyncio
async def test_a_new_viewer_of_a_kept_screen_nudges_the_connection_that_keeps_it(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    set_meta(db, sid, connectionId="conn-old")
    transport[3].clear()
    await session_screen.watch(BOB, sid)
    assert [(c, b["type"]) for c, b in transport[3]] == [("conn-1", "local.session.watch")]


@pytest.mark.asyncio
async def test_a_kept_screen_is_only_for_people_who_can_read_the_session(transport, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    await session_screen.watch(BOB, sid)
    told = []

    async def hint(owner, human, **_):
        told.append((owner, human))

    monkeypatch.setattr(relay_module, "publish_resources_changed", hint)
    # Another workspace's member: the session does not exist for them.
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(AuthContext(user_id="user_eve", org_id="org_other"), sid)
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    # A removed member's still-valid JWT.
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(BOB, sid)
    assert refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    # The owner stops sharing it: gone for a teammate, not streamed, and the Mac stops.
    await service.set_sharing(ALICE, sid, False)
    for person, code in ((BOB, "LOCAL_SESSION_NOT_FOUND"), (ALICE, "LOCAL_SESSION_OFFLINE")):
        with pytest.raises(ApiProblem) as refused:
            await session_screen.watch(person, sid)
        assert refused.value.error.code == code
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == []
    assert (await relay(transport).handle_message("conn-1", screens(sid)))["watching"] == []
    # The screen is still on the Mac: a refused ping must not drop its lease or tell the workspace.
    row = await repo.get_session(OWNER, sid)
    assert int(row["screenUntil"]) > time.time() and told == []
    # Shared again: the same screen is back.
    await service.set_sharing(ALICE, sid, True)
    await session_screen.watch(BOB, sid)


@pytest.mark.asyncio
async def test_a_screen_that_comes_back_tells_the_open_panels_once(transport, db, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid)  # the hook's end came before the holder had its final screen
    told = []

    async def hint(owner, human, **_):
        told.append((owner, human))

    monkeypatch.setattr(relay_module, "publish_resources_changed", hint)
    await relay(transport).handle_message("conn-1", screens(sid))
    assert (await public(sid)).screen_available is True
    assert told == [(OWNER, ALICE.user_id)]
    await relay(transport).handle_message("conn-1", screens(sid))
    assert len(told) == 1, "a renewal is not news"
    set_meta(db, sid, screenUntil=1)  # a heartbeat gap let the lease lapse
    await relay(transport).handle_message("conn-1", screens(sid))
    assert len(told) == 2
