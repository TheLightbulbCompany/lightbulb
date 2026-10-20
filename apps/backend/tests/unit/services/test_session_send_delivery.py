# ruff: noqa: F811
"""Agent network: session senders use the offline inbox, a loop limit, and a delivery word."""

import time
from unittest.mock import patch

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, native_session_tools
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import MAC, registration, relay, start
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER
from tests.unit.services.test_session_sender_hold import _name, wired  # noqa: F401

db = registry_tests.db
transport = relay_tests.transport


async def _send(auth, sid, request_id, **source):
    async def push(*_a, **_k):
        return None

    target = await repo.get_session(OWNER, sid)
    with (
        patch("core.services.push_service.send_push_to_user", push),
        patch("core.services.messaging.human_display_name", _name),
    ):
        return await native_session_tools.session_send(auth, target, "go", request_id, **source)


def _from(source, hop=1):
    return dict(
        source_session_id=source,
        source_generation="generation-1",
        source_turn_id="turn-1",
        native_hop=hop,
        source_pairing=(ALICE.user_id, MAC),
    )


async def _two_sessions(transport):
    source = await start(transport)
    target = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2")))[
        "sessionId"
    ]
    return source, target


def _offline(sid):
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )


@pytest.mark.asyncio
async def test_session_sender_to_offline_mac_is_queued_not_refused(wired):
    source, target = await _two_sessions(wired)
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    _offline(target)
    receipt = await _send(ALICE, target, "q-1", **_from(source))
    assert receipt["state"] == "pending" and receipt["delivery"] == "queued"


@pytest.mark.asyncio
async def test_hosted_mcp_send_to_offline_session_is_queued(wired):
    sid = await start(wired)
    _offline(sid)
    receipt = await _send(ALICE, sid, "h-1", via_client="claude")
    assert receipt["state"] == "pending" and receipt["delivery"] == "queued"


@pytest.mark.asyncio
async def test_thirteenth_message_in_ten_minutes_is_refused(wired):
    sid = await start(wired)
    for i in range(12):
        await _send(ALICE, sid, f"l-{i}", via_client="claude")
    with pytest.raises(ApiProblem, match="Too many messages"):
        await _send(ALICE, sid, "l-12", via_client="claude")
    # A different sender identity has its own budget.
    await _send(ALICE, sid, "other", via_client="codex")


@pytest.mark.asyncio
async def test_loop_window_reopens_after_600s(wired):
    sid = await start(wired)
    for i in range(12):
        await _send(ALICE, sid, f"w-{i}", via_client="claude")
    with patch("time.time", return_value=time.time() + 601):
        await _send(ALICE, sid, "w-12", via_client="claude")


@pytest.mark.asyncio
async def test_delivery_word(wired):
    sid = await start(wired)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    live = await _send(ALICE, sid, "d-live", via_client="claude")
    held = await _send(BOB, sid, "d-held")
    assert live["delivery"] == "now" and held["delivery"] == "waiting for owner"


@pytest.mark.asyncio
async def test_third_session_reply_is_admitted(wired):
    source, target = await _two_sessions(wired)
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    for i in range(3):
        receipt = await _send(ALICE, target, f"hop-{i}", **_from(source))
        assert receipt["state"] in {"pending", "dispatched"}


async def _reconnect_and_dispatch(transport, sid):
    _online = {
        "Key": {"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        "UpdateExpression": "SET onlineUntil = :future",
        "ExpressionAttributeValues": {":future": int(time.time()) + 600},
    }
    messaging_repo._table().update_item(**_online)
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    return [b["command"]["requestId"] for _, b in transport[3] if b["type"] == "local.session.command"]


@pytest.mark.asyncio
async def test_offline_queued_session_send_is_delivered_after_reconnect(wired):
    source, target = await _two_sessions(wired)
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    _offline(target)
    await _send(ALICE, target, "q-s", **_from(source))
    with patch("time.time", return_value=time.time() + 120):  # past the 30 s window
        assert "q-s" in await _reconnect_and_dispatch(wired, target)


@pytest.mark.asyncio
async def test_offline_queued_hosted_send_is_delivered_after_reconnect(wired):
    sid = await start(wired)
    _offline(sid)
    await _send(ALICE, sid, "q-h", via_client="claude")
    with patch("time.time", return_value=time.time() + 120):
        assert "q-h" in await _reconnect_and_dispatch(wired, sid)


@pytest.mark.asyncio
async def test_retry_of_the_twelfth_send_returns_its_receipt(wired):
    sid = await start(wired)
    for i in range(12):
        await _send(ALICE, sid, f"r-{i}", via_client="claude")
    again = await _send(ALICE, sid, "r-11", via_client="claude")
    assert again["state"] in {"pending", "dispatched"}


@pytest.mark.asyncio
async def test_held_rows_do_not_fill_the_owners_offline_queue(wired):
    sid = await start(wired)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    _offline(sid)
    for i in range(3):
        await _send(BOB, sid, f"hq-{i}")
    with patch.object(service, "OFFLINE_SEND_PENDING_LIMIT", 3):
        receipt = await _send(ALICE, sid, "own", via_client="claude")
    assert receipt["delivery"] == "queued"


@pytest.mark.asyncio
async def test_unallowed_send_to_offline_session_waits_and_is_not_dispatched(wired):
    sid = await start(wired)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    _offline(sid)
    receipt = await _send(BOB, sid, "hold-off")
    assert receipt["state"] == "awaiting_approval" and receipt["delivery"] == "waiting for owner"
    assert await _reconnect_and_dispatch(wired, sid) == []
