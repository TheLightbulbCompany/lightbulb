"""Agent network §6: a session send from a person the owner has not allowed waits for the owner.

Real Moto transactions through the relay, as in the team-control tests.
"""

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, native_session_tools
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_controls import CAROL, NAMES, decide
from tests.unit.services.test_local_session_relay import relay, start
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

db = registry_tests.db
transport = relay_tests.transport


@pytest.fixture
def wired(transport, monkeypatch):
    """The relay the send and the owner's Allow dispatch through is this test's relay."""
    from core.services import local_session_relay

    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay(transport))
    return transport


async def _name(user_id):
    return NAMES.get(user_id, user_id)


async def _send(auth, sid, request_id, text="run the tests", **source):
    async def push(*_args, **_kwargs):
        return None

    target = await repo.get_session(OWNER, sid)
    with (
        patch("core.services.push_service.send_push_to_user", push),
        patch("core.services.messaging.human_display_name", _name),
    ):
        return await native_session_tools.session_send(auth, target, text, request_id, **source)


def _delivered(transport):
    return [body["command"]["requestId"] for _, body in transport[3] if body["type"] == "local.session.command"]


async def _states(sid, user_id):
    return [c["state"] for c in await repo.list_commands(OWNER, sid) if c.get("actorUserId") == user_id]


@pytest.mark.asyncio
async def test_unallowed_session_sender_is_held_and_files_ask_to_join(wired):
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    transport[3].clear()
    receipt = await _send(BOB, sid, "held-1")
    assert receipt["state"] == "awaiting_approval"
    row = await repo.get_session(OWNER, sid)
    assert row["joinRequests"][BOB.user_id]["state"] == "pending"
    await relay(transport).dispatch_session(OWNER, sid)
    assert _delivered(transport) == []  # nothing reached the owner's Mac
    # The sender reads the held receipt too.
    assert (await service.get_command(BOB, None, sid, "held-1")).state == "awaiting_approval"


@pytest.mark.asyncio
async def test_allow_releases_held_commands_once(wired):
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await _send(BOB, sid, "held-1")
    transport[3].clear()
    await decide(ALICE, sid, BOB.user_id, "allow", [])
    await decide(ALICE, sid, BOB.user_id, "allow", [])  # a repeat is a no-op
    assert await service.release_held(OWNER, sid, BOB.user_id, "allow") == 0  # a racing second Allow
    assert _delivered(transport) == ["held-1"]
    command = await repo.get_command(OWNER, sid, "held-1")
    assert "heldFor" not in command and command["state"] == "dispatched"


@pytest.mark.asyncio
async def test_decline_rejects_held_and_never_delivers(wired):
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await _send(BOB, sid, "held-1")
    transport[3].clear()
    await decide(ALICE, sid, BOB.user_id, "decline", [])
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "rejected"
    await relay(transport).dispatch_session(OWNER, sid)
    assert _delivered(transport) == []


@pytest.mark.asyncio
async def test_removed_member_held_command_is_not_released(wired):
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await _send(BOB, sid, "held-1")
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    transport[3].clear()
    await decide(ALICE, sid, BOB.user_id, "allow", [])
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "rejected"
    assert _delivered(transport) == []


@pytest.mark.asyncio
async def test_own_agents_and_allowed_people_are_not_held(wired):
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    assert (await _send(ALICE, sid, "own-1"))["state"] != "awaiting_approval"
    with patch("core.services.messaging.human_display_name", _name):
        await service.request_join(BOB, sid)
    await decide(ALICE, sid, BOB.user_id, "allow", [])
    assert (await _send(BOB, sid, "bob-1"))["state"] != "awaiting_approval"
    assert "heldFor" not in await repo.get_command(OWNER, sid, "bob-1")
    # Carol, never allowed, is the only one held; nobody else's ask was filed.
    assert (await _send(CAROL, sid, "carol-1"))["state"] == "awaiting_approval"
    assert set((await repo.get_session(OWNER, sid))["joinRequests"]) == {CAROL.user_id}


@pytest.mark.asyncio
async def test_hosted_mcp_session_send_from_unallowed_person_is_held(monkeypatch):
    from core.services import hosted_mcp

    sid = str(uuid.uuid4())
    target = {"sessionId": sid, "ownerId": OWNER, "humanUserId": ALICE.user_id, "generation": "g1"}
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_scope: BOB)
    monkeypatch.setattr(hosted_mcp, "_mac", lambda: None)
    monkeypatch.setattr(
        hosted_mcp,
        "get_access_token",
        lambda: SimpleNamespace(client_id="https://claude.ai/oauth/claude-code-client-metadata"),
    )
    monkeypatch.setattr(hosted_mcp, "_pin_destination", AsyncMock(return_value={}))
    monkeypatch.setattr(hosted_mcp, "_record_outcome", AsyncMock())
    authorize = AsyncMock(return_value=(target, {}))
    monkeypatch.setattr(hosted_mcp.local_sessions, "authorize_session", authorize)
    join = AsyncMock(return_value=(target, True))
    monkeypatch.setattr(native_session_tools.local_sessions, "file_join_request", join)
    announce = AsyncMock()
    monkeypatch.setattr(native_session_tools.local_sessions, "announce_held_send", announce)
    submit = AsyncMock(return_value=SimpleNamespace(state="awaiting_approval"))
    monkeypatch.setattr(native_session_tools.local_sessions, "submit_command", submit)
    monkeypatch.setattr(native_session_tools.sessions_repo, "list_commands", AsyncMock(return_value=[]))
    from core.services import local_session_relay

    monkeypatch.setattr(
        local_session_relay, "get_local_session_relay", lambda: SimpleNamespace(dispatch_session=AsyncMock())
    )
    result = await hosted_mcp.send_message("hi", uuid.uuid4(), session_id=sid)
    assert result["state"] == "awaiting_approval"
    authorize.assert_awaited_once_with(BOB, None, sid)  # a read, not owner_only
    join.assert_awaited_once_with(BOB, sid)
    announce.assert_awaited_once()  # after the command: the presence push then carries the message
    assert submit.await_args.kwargs["hold_for"] == BOB.user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [False, True])
async def test_a_released_native_send_outlives_the_turn_that_wrote_it(db, wired, released):
    """Only the owner's Allow (``releasedAt``) lifts the source turn's 30 s window; nothing else does."""
    from tests.unit.services.test_local_session_relay import MAC, heartbeat, registration
    from tests.unit.services.test_local_sessions import command

    transport = wired
    source = await start(transport)
    target = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2")))[
        "sessionId"
    ]
    sent = await service.submit_command(
        ALICE,
        None,
        target,
        command(service),
        source_session_id=source,
        source_generation="generation-1",
        source_turn_id="turn-1",
        native_hop=1,
        source_pairing=(ALICE.user_id, MAC),
    )
    await relay(transport).handle_message("conn-1", heartbeat(source, activeTurnId="turn-2"))  # the turn moved on
    key = {"pk": repo.session_pk(OWNER, target), "sk": f"COMMAND#{sent.request_id}"}
    changes = "SET invocationExpiresAt = :past" + (", releasedAt = :past" if released else "")
    db[0].update_item(Key=key, UpdateExpression=changes, ExpressionAttributeValues={":past": 1})
    await relay(transport).dispatch_session(OWNER, target)
    state = (await repo.get_command(OWNER, target, sent.request_id))["state"]
    assert state == ("dispatched" if released else "rejected")
    if released:
        # What the Mac receives: its own `authorized_at` refuses a session actor past this.
        (envelope,) = _envelopes(transport, sent.request_id)
        assert envelope["invocationExpiresAt"] > time.time()


def _envelopes(transport, request_id):
    return [
        body["command"]
        for _, body in transport[3]
        if body["type"] == "local.session.command" and body["command"]["requestId"] == request_id
    ]


async def _hold_for_bob(db, target, request_id, **extra):
    """Rewrite a native send into the shape session_send stores for an unallowed Bob."""
    key = {"pk": repo.session_pk(OWNER, target), "sk": f"COMMAND#{request_id}"}
    sets = ", ".join(f"{k} = :{k}" for k in extra)
    db[0].update_item(
        Key=key,
        UpdateExpression="SET #s = :held, heldFor = :bob, sourceHumanUserId = :bob, deliverBy = :later"
        + (", " + sets if sets else ""),
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":held": "awaiting_approval",
            ":bob": BOB.user_id,
            ":later": int(time.time()) + service.ASK_TTL_S,
            **{f":{k}": v for k, v in extra.items()},
        },
    )


@pytest.mark.asyncio
async def test_a_send_allowed_a_minute_later_reaches_the_mac_unexpired(db, wired):
    from tests.unit.services.test_local_session_relay import heartbeat

    transport = wired
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    _, target, request_id = await _same_mac_native_send(transport)
    await _hold_for_bob(db, target, request_id)
    later = time.time() + 60
    with patch("time.time", return_value=later):
        await relay(transport).handle_message("conn-1", heartbeat(target))
        transport[3].clear()
        with patch("core.services.messaging.human_display_name", _name):
            await service.request_join(BOB, target)
        await decide(ALICE, target, BOB.user_id, "allow", [])
    (envelope,) = _envelopes(transport, request_id)
    assert envelope["invocationExpiresAt"] > later


@pytest.mark.asyncio
async def test_an_offline_queued_session_send_reaches_the_mac_unexpired_after_reconnect(db, wired):
    from tests.unit.services.test_local_session_relay import MAC, connect, registration
    from tests.unit.services.test_local_sessions import command

    transport = wired
    source = await start(transport)
    target = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2")))[
        "sessionId"
    ]
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, target), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    sent = await service.submit_command(
        ALICE,
        None,
        target,
        command(service),
        source_session_id=source,
        source_generation="generation-1",
        source_turn_id="turn-1",
        native_hop=1,
        source_pairing=(ALICE.user_id, MAC),
    )
    assert sent.state == "pending" and sent.deliver_by - sent.created_at == service.OFFLINE_SEND_SECONDS
    connect(transport, "conn-2", "2000")
    later = time.time() + 60
    with patch("time.time", return_value=later):
        await relay(transport).handle_message("conn-2", registration(nativeConversationId="thread-2", status="idle"))
    (envelope,) = _envelopes(transport, sent.request_id)
    assert envelope["invocationExpiresAt"] > later


@pytest.mark.asyncio
async def test_allowing_an_offline_queued_held_send_keeps_the_offline_wait(db, wired):
    transport = wired
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    _, target, request_id = await _same_mac_native_send(transport)
    await _hold_for_bob(db, target, request_id, offlineIdentity={"deviceId": "x"})
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, target), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    now = int(time.time())
    with patch("core.services.messaging.human_display_name", _name):
        await service.request_join(BOB, target)
    await decide(ALICE, target, BOB.user_id, "allow", [])  # the target's Mac is offline
    row = await repo.get_command(OWNER, target, request_id)
    assert row["state"] == "pending" and row["deliverBy"] >= now + service.OFFLINE_SEND_SECONDS


@pytest.mark.asyncio
async def test_held_sends_are_capped_per_sender_and_target(wired):
    from core.services.messaging import ApiProblem

    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    with patch.object(service, "OFFLINE_SEND_PENDING_LIMIT", 2):
        await _send(BOB, sid, "held-1")
        await _send(BOB, sid, "held-2")
        with pytest.raises(ApiProblem, match="waiting for the owner"):
            await _send(BOB, sid, "held-3")


async def _same_mac_native_send(transport):
    from tests.unit.services.test_local_session_relay import MAC, registration
    from tests.unit.services.test_local_sessions import command

    source = await start(transport)
    target = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2")))[
        "sessionId"
    ]
    sent = await service.submit_command(
        ALICE,
        None,
        target,
        command(service),
        source_session_id=source,
        source_generation="generation-1",
        source_turn_id="turn-1",
        native_hop=1,
        source_pairing=(ALICE.user_id, MAC),
    )
    return source, target, sent.request_id


@pytest.mark.asyncio
async def test_a_released_send_still_fails_when_the_targets_generation_changed(db, wired):
    from tests.unit.services.test_local_session_relay import registration

    transport = wired
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    _, target, request_id = await _same_mac_native_send(transport)
    key = {"pk": repo.session_pk(OWNER, target), "sk": f"COMMAND#{request_id}"}
    db[0].update_item(Key=key, UpdateExpression="SET releasedAt = :one", ExpressionAttributeValues={":one": 1})
    await relay(transport).handle_message(
        "conn-1",
        {
            **registration(nativeConversationId="thread-2", generation="generation-2"),
            "previousGeneration": "generation-1",
        },
    )
    await relay(transport).dispatch_session(OWNER, target)
    assert (await repo.get_command(OWNER, target, request_id))["state"] == "rejected"


@pytest.mark.asyncio
async def test_a_held_send_is_delivered_after_its_senders_session_went_offline(db, wired):
    """A teammate who closed their terminal before the owner allowed them still gets the message there."""
    transport = wired
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    source, target, request_id = await _same_mac_native_send(transport)
    # Bob's session wrote it and Alice has not allowed Bob: the shape session_send stores.
    key = {"pk": repo.session_pk(OWNER, target), "sk": f"COMMAND#{request_id}"}
    db[0].update_item(
        Key=key,
        UpdateExpression="SET #s = :held, heldFor = :bob, sourceHumanUserId = :bob, deliverBy = :later",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":held": "awaiting_approval", ":bob": BOB.user_id, ":later": 2**40},
    )
    # The sending session goes offline.
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, source), "sk": "META"},
        UpdateExpression="SET onlineUntil = :zero, #s = :offline",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":zero": 0, ":offline": "offline"},
    )
    transport[3].clear()
    with patch("core.services.messaging.human_display_name", _name):
        await service.request_join(BOB, target)
    await decide(ALICE, target, BOB.user_id, "allow", [])
    assert (await repo.get_command(OWNER, target, request_id))["state"] == "dispatched"
    assert _delivered(transport) == [request_id]


def _listed(owner="user_alice", **over):
    return SimpleNamespace(
        **{
            "shared": True,
            "session_id": str(uuid.uuid4()),
            "provider": "codex",
            "display_title": None,
            "title": "Target",
            "cwd_name": "repo",
            "status": "idle",
            "capabilities_verified": True,
            "capabilities": SimpleNamespace(send_mode="queue"),
            "control_unavailable_reason": None,
            "human_user_id": owner,
            "team_control": False,
            "allowed_senders": [],
            **over,
        }
    )


@pytest.mark.asyncio
async def test_find_sessions_and_list_targets_offer_held_sends_and_say_they_wait(monkeypatch):
    from core.services import hosted_mcp

    stranger, allowed, mine = _listed(), _listed(allowed_senders=[BOB.user_id]), _listed(owner=BOB.user_id)
    only_me = _listed(owner=BOB.user_id, shared=False)
    sessions = [stranger, allowed, mine, only_me]
    monkeypatch.setattr(
        native_session_tools.local_sessions,
        "list_workspace_sessions",
        AsyncMock(return_value=SimpleNamespace(sessions=sessions)),
    )
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Someone"))
    monkeypatch.setattr(native_session_tools.messaging, "list_rooms", AsyncMock(return_value=SimpleNamespace(rooms=[])))

    found = (await hosted_mcp.sessions_for(BOB))["sessions"]
    assert [(s["canSend"], s["needsOwnerOk"]) for s in found] == [
        (True, True),
        (True, False),
        (True, False),
        (False, False),
    ]
    targets = (await native_session_tools._targets(BOB, "source"))["sessions"]
    assert [(t["destination"], t["needsOwnerOk"]) for t in targets] == [
        (f"session:{stranger.session_id}", True),
        (f"session:{allowed.session_id}", False),
        (f"session:{mine.session_id}", False),
    ]
    listed = {tool.name: tool for tool in await hosted_mcp.mcp.list_tools()}
    assert "needsOwnerOk" in listed["find_sessions"].description


async def _held_bob(transport):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await _send(BOB, sid, "held-1")
    transport[3].clear()
    return sid


def _stale_once(monkeypatch, snapshot):
    """The next list_commands (release_held's read) returns ``snapshot``; every later read is real."""
    real = repo.list_commands
    calls = []

    async def listed(owner, sid):
        if not calls:
            calls.append(1)
            return snapshot
        return await real(owner, sid)

    monkeypatch.setattr(repo, "list_commands", listed)


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_via", ["dispatch", "receipt"])
async def test_an_expired_held_send_is_never_revived_by_a_later_allow(wired, expire_via):
    import time

    transport = wired
    sid = await _held_bob(transport)
    later = time.time() + service.ASK_TTL_S + 5
    with patch("time.time", return_value=later):
        if expire_via == "dispatch":
            await relay(transport).dispatch_session(OWNER, sid)
        else:
            assert (await service.get_command(BOB, None, sid, "held-1")).state == "rejected"
        expired = await repo.get_command(OWNER, sid, "held-1")
        assert expired["state"] == "rejected" and "heldFor" not in expired
        with patch("core.services.messaging.human_display_name", _name):
            await service.request_join(BOB, sid)  # the person asks again
        await decide(ALICE, sid, BOB.user_id, "allow", [])
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "rejected"
    assert _delivered(transport) == []


@pytest.mark.asyncio
async def test_two_releases_over_one_snapshot_deliver_once(wired, monkeypatch):
    transport = wired
    sid = await _held_bob(transport)
    snapshot = await repo.list_commands(OWNER, sid)
    await decide(ALICE, sid, BOB.user_id, "allow", [])  # the first release wins the write
    _stale_once(monkeypatch, snapshot)
    assert await service.release_held(OWNER, sid, BOB.user_id, "allow") == 0  # its conditional write loses
    assert _delivered(transport) == ["held-1"]
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "dispatched"


@pytest.mark.asyncio
async def test_an_allow_racing_the_expiry_never_delivers(wired, monkeypatch):
    import time

    transport = wired
    sid = await _held_bob(transport)
    snapshot = await repo.list_commands(OWNER, sid)  # the Allow read it while still held
    with patch("time.time", return_value=time.time() + service.ASK_TTL_S + 5):
        await relay(transport).dispatch_session(OWNER, sid)  # the expiry lands first
    with patch("core.services.messaging.human_display_name", _name):
        await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    _stale_once(monkeypatch, snapshot)
    assert await service.release_held(OWNER, sid, BOB.user_id, "allow") == 0
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "rejected"
    assert _delivered(transport) == []


@pytest.mark.asyncio
async def test_release_reads_the_session_not_the_callers_decision(wired):
    transport = wired
    sid = await _held_bob(transport)
    # Nobody allowed Bob: an "allow" from a caller is not enough, and the held send is rejected.
    transport[4].clear()
    assert await service.release_held(OWNER, sid, BOB.user_id, "allow") == 1
    assert (await repo.get_command(OWNER, sid, "held-1"))["state"] == "rejected"
    assert _delivered(transport) == []
    hints = [h for h in transport[4] if "workspace.sessions.changed" in str(h)]
    assert hints  # the sender's client learns from the hint, without polling


@pytest.mark.asyncio
async def test_each_held_send_is_announced_as_a_message_after_its_command_is_stored(wired):
    """The owner hears "wants to message", with each message, once its command exists (so the
    presence push carries it to the Mac's notification), and the desktop window leaves the
    banner to the Mac. A second message is announced too, without a second Needs-you item."""
    transport = wired
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    notices, held_at_notice = AsyncMock(), []

    async def presence(row):
        held_at_notice.append(await _states(sid, BOB.user_id))

    with patch.object(service, "_session_notice", notices), patch.object(service, "_push_presence", presence):
        await _send(BOB, sid, "held-copy-1", text="rebase on main please")
        await _send(BOB, sid, "held-copy-2", text="and run the tests")
    assert [c.args[5] for c in notices.await_args_list] == ["rebase on main please", "and run the tests"]
    assert all("wants to message" in c.args[4] and sid[:8] not in c.args[4] for c in notices.await_args_list)
    assert all(c.kwargs == {"held": True} for c in notices.await_args_list)
    assert held_at_notice[0] == ["awaiting_approval"]  # the command was stored before the push
    rows = await messaging_repo._query(messaging_repo.activity_pk(OWNER, ALICE.user_id))
    assert len([r for r in rows if r.get("subtype") == "join_request"]) == 1
