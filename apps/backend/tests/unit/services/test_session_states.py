"""Ended screens and terminal reasons (spec 2026-10-07 §5.1, §5.4): what the Mac reports and
what every reader is shown."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import time

import pytest

from core.repositories import local_sessions_repo as repo
from core.services import local_session_relay as relay_module, local_sessions as service
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
from tests.unit.services.test_local_sessions import BOB, OWNER
from tests.unit.services.test_session_screen import (  # noqa: F401 -- autouse fixtures
    _nudges_reach_the_test_mac,
    _teammates_are_members,
    ack,
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
