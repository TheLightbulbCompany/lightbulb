"""Agents ask before messaging a person's session (spec 2026-10-07-agent-session-approval):
real DynamoDB conditions under Moto, notifications and delivery stubbed."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import local_sessions_repo as repo
from core.services import local_sessions as service
from core.services.messaging import ApiProblem
from tests.unit.services.test_local_sessions import (  # noqa: F401 -- db is a fixture
    ALICE,
    BOB,
    OTHER_ROOM,
    OWNER,
    ROOM,
    command,
    db,
    registered,
)

AGENT = "33333333-3333-4333-8333-333333333333"
KEY = "hermetic-signing-key"


def test_paperclip_grant_round_trips_and_keeps_a_strict_shape(monkeypatch):
    import jwt
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", KEY)
    hermes_key = f"paperclip:company:c-1:agent:{AGENT}:run:run-1"
    token = grants.mint_paperclip(OWNER, AGENT, hermes_key, "run-1", None)
    caller = grants.verify(token)
    assert (caller.room_id, caller.requester_id, caller.turn_id) == (None, None, "run-1")
    assert caller.origin == {"kind": "paperclip", "runId": "run-1", "issueId": None}
    original = jwt.decode(token, options={"verify_signature": False})
    for changes in (
        {"extra": 1},
        {"exp": original["iat"] + 1801},
        {"requesterId": ALICE.user_id},
        {"roomId": ROOM},
        {"sessionKey": "paperclip:company:c-1:agent:other:run:run-1"},
        {"sessionKey": f"paperclip:company:c-1:agent:{AGENT}:run:run-2"},
        {"origin": {"kind": "paperclip", "runId": "run-1"}},
    ):
        with pytest.raises(ApiProblem):
            grants.verify(jwt.encode({**original, **changes}, KEY, algorithm="HS256"))
    with pytest.raises(ApiProblem):
        grants.verify(jwt.encode({k: v for k, v in original.items() if k != "eventId"}, KEY, algorithm="HS256"))
    # A room grant an older task minted (no `origin`) still verifies during a roll.
    room = jwt.decode(grants.mint(OWNER, ROOM, AGENT, OTHER_ROOM), options={"verify_signature": False})
    old = grants.verify(jwt.encode({k: v for k, v in room.items() if k != "origin"}, KEY, algorithm="HS256"))
    assert (old.room_id, old.turn_id, old.origin) == (ROOM, OTHER_ROOM, {"kind": "room", "roomId": ROOM})


@pytest.fixture
def asks(db, monkeypatch):  # noqa: F811
    """An agent seated in ROOM; ALICE's session; notifications and delivery stubbed."""
    from core.services import local_session_grants as grants, local_session_relay, website_logins

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", KEY)
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{AGENT}", "kind": "agent", "id": AGENT})
    monkeypatch.setattr(website_logins, "_agent_name", AsyncMock(return_value="Friday"))
    real_notify, notify = service._notify_agent_ask, AsyncMock()
    monkeypatch.setattr(service, "_notify_agent_ask", notify)
    monkeypatch.setattr(service, "_push_presence", AsyncMock())
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", AsyncMock())
    relay = SimpleNamespace(dispatch_session=AsyncMock())
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)

    def grant(requester=BOB.user_id, event="44444444-4444-4444-8444-444444444444"):
        return grants.verify(grants.mint(OWNER, ROOM, AGENT, event, requester))

    def paperclip(run="run-1"):
        key = f"paperclip:company:c-1:agent:{AGENT}:run:{run}"
        return grants.verify(grants.mint_paperclip(OWNER, AGENT, key, run, None))

    return SimpleNamespace(
        grant=grant, paperclip=paperclip, notify=notify, real_notify=real_notify, relay=relay, table=db[0]
    )


async def _prefs(user, **prefs):
    from core.repositories import user_repo

    if not await user_repo.get(user):
        await user_repo.put(user, f"{user}@example.com")
    await user_repo.set_prefs(user, **prefs)


async def _command_ids(sid):
    return [c["requestId"] for c in await repo.list_commands(OWNER, sid)]


def _expire(table, sid, request_id):
    """24 h pass: the ask and the agent's slot share one expiry."""
    for sk in (f"AGENTASK#{request_id}", f"AGENTASKSLOT#{AGENT}"):
        table.update_item(
            Key={"pk": repo.session_pk(OWNER, sid), "sk": sk},
            UpdateExpression="SET expiresAt = :past",
            ExpressionAttributeValues={":past": int(time.time()) - 1},
        )


@pytest.mark.asyncio
async def test_requester_owned_send_goes_straight_in_and_files_no_ask(asks):
    sid = await registered()
    receipt = await service.agent_command(asks.grant(ALICE.user_id), sid, command(service))
    assert not isinstance(receipt, service.AgentCommandReceipt) and receipt.state == "pending"
    assert await service.pending_agent_asks(OWNER, sid) == []
    asks.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_owner_send_files_one_ask_and_nothing_reaches_the_mac(asks):
    sid = await registered()
    caller = asks.grant()
    receipt = await service.agent_command(caller, sid, command(service))
    assert isinstance(receipt, service.AgentCommandReceipt)
    public = receipt.model_dump(by_alias=True)
    assert public["state"] == "awaiting_approval" and public["expiresAt"] > time.time() + 23 * 3600
    assert public["actor"] == {"kind": "agent", "id": AGENT} and "reason" not in public
    assert await _command_ids(sid) == []
    asks.relay.dispatch_session.assert_not_awaited()
    asks.notify.assert_awaited_once()
    again = await service.agent_command(caller, sid, command(service))
    assert again == receipt and len(await service.pending_agent_asks(OWNER, sid)) == 1
    with pytest.raises(ApiProblem) as conflict:
        await service.agent_command(caller, sid, command(service, text="something else"))
    assert conflict.value.error.code == "IDEMPOTENCY_CONFLICT"
    assert (await service.get_agent_receipt(caller, sid, "request-1")).state == "awaiting_approval"


@pytest.mark.asyncio
async def test_send_settles_once_and_a_crash_after_the_flip_resumes(asks):
    sid = await registered()
    await service.agent_command(asks.grant(), sid, command(service))
    receipt = await service.decide_agent_ask(ALICE, sid, "request-1", "send")
    assert receipt.state == "pending" and receipt.approved_by == ALICE.user_id
    [stored] = await repo.list_commands(OWNER, sid)
    assert stored["actor"] == {"kind": "agent", "id": AGENT} and stored["approvedBy"] == ALICE.user_id
    assert stored["roomId"] == ROOM
    asks.relay.dispatch_session.assert_awaited_once_with(OWNER, sid)
    # The relay restores an agent a person approved, and re-checks it at delivery.
    restored = service.command_caller(OWNER, stored)
    assert restored.approved_by == ALICE.user_id
    await service.authorize_session(restored, ROOM, sid, owner_only=True)
    with pytest.raises(ApiProblem) as loser:
        await service.decide_agent_ask(ALICE, sid, "request-1", "send")
    assert loser.value.error.code == "LOCAL_SESSION_ASK_SETTLED"
    assert loser.value.error.details["receipt"]["state"] == "pending"
    assert await _command_ids(sid) == ["request-1"]

    # Two Sends at once: exactly one command, and anyone who lost hears 409.
    await service.agent_command(asks.grant(), sid, command(service, requestId="request-2"))
    results = await asyncio.gather(
        *(service.decide_agent_ask(ALICE, sid, "request-2", "send") for _ in range(2)), return_exceptions=True
    )
    assert (await _command_ids(sid)).count("request-2") == 1
    assert any(isinstance(r, service.AgentCommandReceipt) for r in results)
    assert all(
        isinstance(r, service.AgentCommandReceipt) or r.error.code == "LOCAL_SESSION_ASK_SETTLED" for r in results
    )

    # A crash between the flip and the command leaves `sent` with no COMMAND#: the next Send admits it.
    await service.agent_command(asks.grant(), sid, command(service, requestId="request-3"))
    assert await service._settle_ask(await service._get_ask(OWNER, sid, "request-3"), "sent", ALICE.user_id)
    assert "request-3" not in await _command_ids(sid)
    assert (await service.decide_agent_ask(ALICE, sid, "request-3", "send")).state == "pending"
    assert (await _command_ids(sid)).count("request-3") == 1

    # Stranded again: Activity keeps it open with Send only, and the agent's own receipt read admits it.
    from core.services import activity

    await service.agent_command(asks.grant(), sid, command(service, requestId="request-4"))
    assert await service._settle_ask(await service._get_ask(OWNER, sid, "request-4"), "sent", ALICE.user_id)
    ask = await service._get_ask(OWNER, sid, "request-4")
    session = await repo.get_session(OWNER, sid)
    item = activity.render_agent_ask(ask, session, seen=None, stranded=await activity._ask_stranded(OWNER, ask))
    assert item.state == "open" and [a.id for a in item.actions] == ["send"]
    assert (await service.get_agent_receipt(asks.grant(), sid, "request-4")).state == "pending"
    assert (await _command_ids(sid)).count("request-4") == 1


@pytest.mark.asyncio
async def test_a_removed_members_session_is_gone_to_agents(asks):
    from core.repositories import messaging_repo

    sid = await registered()
    caller = asks.grant(ALICE.user_id)
    receipt = await service.agent_command(caller, sid, command(service))
    stored = await repo.get_command(OWNER, sid, receipt.request_id)
    asks.table.update_item(
        Key={"pk": messaging_repo.owner_pk(OWNER), "sk": messaging_repo.userstream_sk(ALICE.user_id)},
        UpdateExpression="SET active = :off",
        ExpressionAttributeValues={":off": False},
    )
    with pytest.raises(ApiProblem) as gone:
        await service.list_events(caller, ROOM, sid)
    assert gone.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    # Delivery is fenced in the transaction too, not only at admission.
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        await messaging_repo._transact(repo.room_conditions(OWNER, ALICE.user_id, stored))


@pytest.mark.asyncio
async def test_decline_and_expiry_read_as_rejected_and_cannot_be_answered_again(asks):
    sid = await registered()
    caller = asks.grant()
    await service.agent_command(caller, sid, command(service))
    declined = await service.decide_agent_ask(ALICE, sid, "request-1", "decline")
    assert (declined.state, declined.reason) == ("rejected", "declined")
    assert (await service.get_agent_receipt(caller, sid, "request-1")).reason == "declined"
    assert await _command_ids(sid) == []
    with pytest.raises(ApiProblem, match="already answered"):
        await service.decide_agent_ask(ALICE, sid, "request-1", "send")

    later = asks.grant(event="55555555-5555-4555-8555-555555555555")
    await service.agent_command(later, sid, command(service, requestId="request-2"))
    _expire(asks.table, sid, "request-2")
    expired = await service.get_agent_receipt(later, sid, "request-2")
    assert (expired.state, expired.reason) == ("rejected", "expired")
    assert await service.pending_agent_asks(OWNER, sid) == []
    with pytest.raises(ApiProblem) as late:
        await service.decide_agent_ask(ALICE, sid, "request-2", "send")
    assert late.value.error.code == "LOCAL_SESSION_ASK_SETTLED"
    # An expired ask frees the agent's slot.
    renewed = await service.agent_command(later, sid, command(service, requestId="request-3"))
    assert renewed.state == "awaiting_approval"


@pytest.mark.asyncio
async def test_one_pending_ask_per_agent_and_no_reask_after_a_decline_in_the_same_run(asks):
    sid = await registered(
        capabilities={"sendMode": "immediate", "steer": True, "interrupt": True}, status="busy", activeTurnId="turn-1"
    )
    caller = asks.grant()
    stop = command(service, kind="interrupt", text=None, expectedTurnId="turn-1")
    assert (await service.agent_command(caller, sid, stop)).state == "awaiting_approval"
    steer = command(service, requestId="request-2", expectedTurnId="turn-1")
    with pytest.raises(ApiProblem) as pending:
        await service.agent_command(caller, sid, steer)
    assert pending.value.status_code == 429 and pending.value.error.code == "LOCAL_SESSION_ASK_PENDING"
    await service.decide_agent_ask(ALICE, sid, "request-1", "decline")
    with pytest.raises(ApiProblem) as declined:
        await service.agent_command(caller, sid, steer)
    assert declined.value.error.code == "LOCAL_SESSION_ASK_DECLINED"
    next_run = asks.grant(event="55555555-5555-4555-8555-555555555555")
    assert (await service.agent_command(next_run, sid, steer)).state == "awaiting_approval"


@pytest.mark.asyncio
async def test_team_control_may_approve_an_allowed_sender_may_not_and_the_setting_sends_directly(asks):
    sid = await registered()
    await service.agent_command(asks.grant(), sid, command(service))
    asks.table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET allowedSenders = :b",
        ExpressionAttributeValues={":b": [BOB.user_id]},
    )
    with pytest.raises(ApiProblem, match="team control"):
        await service.decide_agent_ask(BOB, sid, "request-1", "send")
    assert await service.list_agent_asks(BOB, sid) == []
    await service.set_team_control(ALICE, sid, True)
    assert [a.request_id for a in await service.list_agent_asks(BOB, sid)] == ["request-1"]
    assert (await service.decide_agent_ask(BOB, sid, "request-1", "send")).approved_by == BOB.user_id
    [stored] = await repo.list_commands(OWNER, sid)
    assert stored["approvedBy"] == BOB.user_id
    # Team control turned off before delivery: the relay's re-check refuses the teammate's approval.
    await service.set_team_control(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="owner"):
        await service.authorize_session(service.command_caller(OWNER, stored), ROOM, sid, owner_only=True)

    assert (await service.list_resources(asks.grant(), ROOM)).sessions[0].agent_send == "ask"
    await _prefs(ALICE.user_id, agent_send_without_asking=True)
    direct = await service.agent_command(asks.grant(), sid, command(service, requestId="request-2"))
    assert not isinstance(direct, service.AgentCommandReceipt)
    assert (await service.list_resources(asks.grant(), ROOM)).sessions[0].agent_send == "direct"


@pytest.mark.asyncio
async def test_a_stale_ask_fails_at_send_and_is_never_retried(asks):
    sid = await registered()
    await service.agent_command(asks.grant(), sid, command(service))
    await registered(generation="generation-2")
    receipt = await service.decide_agent_ask(ALICE, sid, "request-1", "send")
    assert (receipt.state, receipt.reason) == ("rejected", "LOCAL_SESSION_CONFLICT")
    assert await _command_ids(sid) == []
    with pytest.raises(ApiProblem, match="already answered"):
        await service.decide_agent_ask(ALICE, sid, "request-1", "send")


@pytest.mark.asyncio
async def test_background_work_reaches_only_people_who_turned_it_on(asks):
    sid = await registered()
    run = asks.paperclip()
    assert (await service.list_resources(run, None)).sessions == []
    with pytest.raises(ApiProblem) as hidden:
        await service.list_events(run, None, sid)
    assert hidden.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    with pytest.raises(ApiProblem):
        await service.agent_command(run, sid, command(service))
    await _prefs(ALICE.user_id, sessions_reachable_from_background_work=True)
    listed = await service.list_resources(run, None)
    assert [(s.session_id, s.agent_send) for s in listed.sessions] == [(sid, "ask")]
    assert listed.devices == [] and listed.paired_nodes == []
    assert (await service.list_events(run, None, sid)).events == []
    # A room grant cannot drop its room to pass as a Paperclip run.
    with pytest.raises(ApiProblem):
        await service.list_resources(asks.grant(), None)
    receipt = await service.agent_command(run, sid, command(service))
    assert receipt.state == "awaiting_approval" and receipt.room_id == ""
    await service.decide_agent_ask(ALICE, sid, "request-1", "send")
    [stored] = await repo.list_commands(OWNER, sid)
    assert stored["roomId"] == ""
    await service.set_sharing(ALICE, sid, False)
    assert (await service.list_resources(run, None)).sessions == []


@pytest.mark.asyncio
async def test_a_room_grant_lists_every_shared_session_in_the_workspace(asks):
    sid = await registered()
    asks.table.delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{ALICE.user_id}"})
    listed = await service.list_resources(asks.grant(), ROOM)
    assert [(s.session_id, s.agent_send) for s in listed.sessions] == [(sid, "ask")]
    assert (await service.list_resources(asks.grant(ALICE.user_id), ROOM)).sessions[0].agent_send == "direct"


@pytest.mark.asyncio
async def test_the_ask_reaches_activity_the_room_and_the_terminal_band(asks, monkeypatch):
    from core.repositories import messaging_repo
    from core.services import activity, messaging, native_session_tools as tools, session_presence
    from core.services.machine_auth import MachineIdentity
    from tests.unit.services.test_local_sessions import MAC

    monkeypatch.setattr(service, "_notify_agent_ask", asks.real_notify)
    notice, line = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "_session_notice", notice)
    monkeypatch.setattr(messaging, "create_agent_message", line)
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Alice"))
    sid = await registered(title="Fix the build")
    await service.agent_command(asks.grant(), sid, command(service))

    # Needs-you card + push: an open item with Send / Decline, answered from Activity.
    owner, user, sid_, state, title, body, _ = notice.await_args.args
    assert (owner, user, sid_, state) == (OWNER, ALICE.user_id, sid, "join_request")
    assert (title, body) == ("Friday wants to message Fix the build", "continue")
    rows = await messaging_repo._query(messaging_repo.activity_pk(OWNER, ALICE.user_id))
    [item] = await activity._render_session_rows(OWNER, ALICE.user_id, rows, None)
    assert item.id == f"agentmsg:{sid}:request-1" and item.state == "open"
    assert [a.id for a in item.actions] == ["send", "decline"] and item.summary.preview.text == "continue"
    # The room line, as a progress line from the agent: it wakes and notifies nobody.
    request = line.await_args.args[1]
    assert (str(request.room_id), str(request.agent_id), request.progress) == (ROOM, AGENT, True)
    assert request.text == "I've asked Alice before sending that to their session."
    answered = await activity._run_agent_ask_action(ALICE, sid, "request-1", "send")
    assert answered.state == "resolved" and answered.actions == []
    assert await _command_ids(sid) == ["request-1"]

    # The terminal band: a Mac that advertises agentMessageAsks sees the ask; its Allow is Send.
    later = asks.grant(event="55555555-5555-4555-8555-555555555555")
    await service.agent_command(later, sid, command(service, requestId="request-2"))
    row = await repo.get_session(OWNER, sid)
    assert (await session_presence.entry(row, 1))["joinRequests"] == []
    entry = await session_presence.entry({**row, "deviceFeatures": ["agentMessageAsks"]}, 1)
    assert entry["joinRequests"] == [
        {
            "userId": AGENT,
            "name": "Friday",
            "requestId": "request-2",
            "kind": "agent_message",
            "action": "send",
            "text": "continue",
        }
    ]
    asks.relay.connect = AsyncMock(return_value=({"device_id": MAC, "user_id": ALICE.user_id}, None))
    device = MachineIdentity(ALICE.user_id, OWNER, OWNER, "node", "device", "openclaw-macos", MAC)
    decision = tools.NativeDecisionRequest(
        session_id=sid, generation="generation-1", decision="allow", user_id=AGENT, request_id="request-2"
    )
    await tools.decide(device, decision)
    assert await _command_ids(sid) == ["request-1", "request-2"]


@pytest.mark.asyncio
async def test_an_approved_agent_is_labelled_with_who_approved_it(monkeypatch):
    from core.services import local_session_relay as relay, session_presence

    monkeypatch.setattr(relay, "_agent_roster_name", AsyncMock(return_value="Friday"))
    monkeypatch.setattr(session_presence, "human_display_name", AsyncMock(return_value="Sam"))
    command_row = {"roomId": "", "actor": {"kind": "agent", "id": AGENT}, "approvedBy": ALICE.user_id}
    by_owner = await relay._actor_label(command_row, ALICE.user_id, OWNER)
    assert by_owner == {"kind": "agent", "handle": None, "displayName": "Friday, approved by you"}
    by_teammate = await relay._actor_label({**command_row, "approvedBy": BOB.user_id}, ALICE.user_id, OWNER)
    assert by_teammate["displayName"] == "Friday, approved by Sam"
    unapproved = {k: v for k, v in command_row.items() if k != "approvedBy"}
    assert (await relay._actor_label(unapproved, ALICE.user_id, OWNER))["displayName"] == "Friday"
