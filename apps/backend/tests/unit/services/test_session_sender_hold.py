"""Agent network §6: a session send from a person the owner has not allowed waits for the owner.

Real Moto transactions through the relay, as in the team-control tests.
"""

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
    join = AsyncMock()
    monkeypatch.setattr(native_session_tools.local_sessions, "request_join", join)
    submit = AsyncMock(return_value=SimpleNamespace(state="awaiting_approval"))
    monkeypatch.setattr(native_session_tools.local_sessions, "submit_command", submit)
    from core.services import local_session_relay

    monkeypatch.setattr(
        local_session_relay, "get_local_session_relay", lambda: SimpleNamespace(dispatch_session=AsyncMock())
    )
    result = await hosted_mcp.send_message("hi", uuid.uuid4(), session_id=sid)
    assert result["state"] == "awaiting_approval"
    authorize.assert_awaited_once_with(BOB, None, sid)  # a read, not owner_only
    join.assert_awaited_once_with(BOB, sid)
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
