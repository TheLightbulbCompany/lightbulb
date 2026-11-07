"""Team control, remote approvals and the audit trail (owner ruling 2026-09-25).

Real Moto transactions through the relay; the desktop is the shared wire fixtures.
"""

import json
from unittest.mock import patch

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo
from core.services import activity, local_sessions as service, session_links
from core.services.local_session_relay import _NO_PROMPT, pending_approval
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import connect, fixture, heartbeat, registration, relay, start
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

db = registry_tests.db
transport = relay_tests.transport

APPROVAL = fixture("approval-pending-event")
APPROVAL_ID = APPROVAL["itemId"]
DIGEST = APPROVAL["tool"]["input"]["inputDigest"]


def approve(**changes):
    body = dict(
        requestId="approve-1",
        expectedGeneration="generation-1",
        kind="approve",
        approvalId=APPROVAL_ID,
        decision="allow",
        inputDigest=DIGEST,
    )
    body.update(changes)
    return service.LocalSessionCommand(**body)


def send(request_id="send-1"):
    return service.LocalSessionCommand(
        requestId=request_id, expectedGeneration="generation-1", kind="send", text="commit it"
    )


async def waiting(transport, sid, event=APPROVAL, seq=0):
    """The desktop reports the prompt, then the status that holds it open."""
    batch = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": f"b{seq}",
        "afterSeq": seq,
        "events": [event],
    }
    await relay(transport).handle_message("conn-1", batch)
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="waiting-approval", activeTurnId=None))


async def actions(sid):
    return [(e.action, e.actor_user_id) for e in (await service.list_audit(BOB, sid)).entries]


@pytest.mark.asyncio
async def test_a_teammate_controls_only_while_the_owner_opted_in(transport):
    sid = await start(transport)
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, send())
    # The toggle is the owner's alone.
    with pytest.raises(ApiProblem, match="owner"):
        await service.set_team_control(BOB, sid, True)
    assert (await service.get_session(BOB, sid)).team_control is False

    public = await service.set_team_control(ALICE, sid, True)
    assert public.team_control is True and public.model_dump(by_alias=True)["teamControl"] is True
    receipt = await service.submit_command(BOB, None, sid, send())
    assert receipt.actor_user_id == BOB.user_id
    # The sender reads their own receipt; the owner reads every receipt.
    assert (await service.get_command(BOB, None, sid, "send-1")).request_id == "send-1"
    assert (await service.get_command(ALICE, None, sid, "send-1")).request_id == "send-1"

    await service.set_team_control(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, send("send-2"))
    assert await actions(sid) == [
        ("team_control_on", ALICE.user_id),
        ("send", BOB.user_id),
        ("team_control_off", ALICE.user_id),
    ]


@pytest.mark.asyncio
async def test_a_teammate_command_admitted_under_the_toggle_is_refenced_at_dispatch(transport):
    sid = await start(transport)
    await service.set_team_control(ALICE, sid, True)
    await service.submit_command(BOB, None, sid, send())
    await service.set_team_control(ALICE, sid, False)
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "send-1"))["state"] == "rejected"
    assert transport[3] == []


@pytest.mark.asyncio
async def test_the_toggle_clears_itself_when_the_session_ends(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
        await service.set_team_control(ALICE, sid, True)
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with patch("time.time", return_value=10006):
        await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert "teamControl" not in row
    assert (await service.get_session(BOB, sid)).team_control is False
    assert [a for a, _ in await actions(sid)] == ["team_control_on", "team_control_ended"]
    with pytest.raises(ApiProblem, match="ended"):
        await service.set_team_control(ALICE, sid, True)
    # A resumed conversation starts with the toggle off, too.
    with patch("time.time", return_value=10008):
        await relay(transport).handle_message("conn-1", registration())
    assert (await service.get_session(BOB, sid)).team_control is False


@pytest.mark.asyncio
async def test_an_approval_lands_only_on_the_exact_open_prompt(transport):
    sid = await start(transport)
    # No prompt yet.
    with pytest.raises(ApiProblem, match="waiting"):
        await service.submit_command(ALICE, None, sid, approve())
    await waiting(transport, sid)
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert public["pendingApproval"] == {
        "approvalId": APPROVAL_ID,
        "tool": "Bash",
        "summary": "git push",
        "inputDigest": DIGEST,
        "exact": True,
        "modes": [],
        "full": False,
    }
    for index, (wrong, match) in enumerate(
        (
            ({"approvalId": "appr-other"}, "changed"),
            ({"inputDigest": "e" * 64}, "changed"),
            ({"mode": "acceptEdits"}, "not offered"),
        )
    ):
        with pytest.raises(ApiProblem, match=match):
            await service.submit_command(ALICE, None, sid, approve(requestId=f"bad-{index}", **wrong))
    # A teammate needs the owner's opt-in to answer it too.
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, approve())
    await service.set_team_control(ALICE, sid, True)
    receipt = await service.submit_command(BOB, None, sid, approve())
    wire = receipt.model_dump(by_alias=True)
    assert wire["approvalId"] == APPROVAL_ID and wire["decision"] == "allow" and "mode" not in wire
    # The dispatched envelope carries the answer to the desktop that holds the prompt.
    await relay(transport).dispatch_session(OWNER, sid)
    envelope = transport[3][-1][1]
    assert envelope["type"] == "local.session.command"
    assert envelope["command"]["kind"] == "approve" and envelope["command"]["inputDigest"] == DIGEST
    assert (await actions(sid))[-1] == ("approve", BOB.user_id)
    entry = (await service.list_audit(BOB, sid)).entries[-1]
    assert entry.request_id == "approve-1" and entry.state == "dispatched"


@pytest.mark.asyncio
async def test_allow_similar_and_allow_for_the_conversation_land_only_where_offered(transport):
    sid = await start(transport)
    tool_input = {**APPROVAL["tool"]["input"], "modes": ["session", "similar"], "similar": "git push *"}
    await waiting(transport, sid, {**APPROVAL, "tool": {**APPROVAL["tool"], "input": tool_input}})
    public = (await service.get_session(ALICE, sid)).model_dump(by_alias=True)["pendingApproval"]
    assert public["modes"] == ["session", "similar"] and public["similar"] == "git push *"
    receipt = await service.submit_command(ALICE, None, sid, approve(mode="similar"))
    assert receipt.model_dump(by_alias=True)["mode"] == "similar"
    await service.submit_command(ALICE, None, sid, approve(requestId="approve-2", mode="session"))
    await relay(transport).dispatch_session(OWNER, sid)
    assert transport[3][-1][1]["command"]["mode"] in {"similar", "session"}
    with pytest.raises(ApiProblem, match="not offered"):
        await service.submit_command(ALICE, None, sid, approve(requestId="approve-3", mode="acceptEdits"))


def test_similar_is_never_offered_without_the_rule_it_allows():
    base = {"approvalId": APPROVAL_ID, "tool": "Bash", "inputDigest": DIGEST, "exact": True}
    assert service.PendingApproval(**base, modes=["similar", "session"]).modes == ["session"]
    shown = service.PendingApproval(**base, modes=["similar"], similar="npm run *")
    assert shown.model_dump(by_alias=True)["similar"] == "npm run *"
    assert "similar" not in service.PendingApproval(**base).model_dump(by_alias=True)
    with pytest.raises(ValueError):
        service.PendingApproval(**base, modes=["bypassPermissions"])


@pytest.mark.asyncio
async def test_a_prompt_the_approver_cannot_see_whole_is_deny_only(transport):
    sid = await start(transport)
    inexact = {**APPROVAL, "tool": {**APPROVAL["tool"], "input": {**APPROVAL["tool"]["input"], "exact": False}}}
    await waiting(transport, sid, inexact)
    with pytest.raises(ApiProblem, match="terminal"):
        await service.submit_command(ALICE, None, sid, approve())
    receipt = await service.submit_command(ALICE, None, sid, approve(decision="deny", text="not on main"))
    assert receipt.decision == "deny"
    assert (await actions(sid))[-1] == ("deny", ALICE.user_id)


FULL_INPUT = {"command": "ls\nls"}


def full_prompt(**changes):
    return {
        **APPROVAL,
        "tool": {
            **APPROVAL["tool"],
            "input": {**APPROVAL["tool"]["input"], "exact": False, "full": True, "input": FULL_INPUT, **changes},
        },
    }


@pytest.mark.asyncio
async def test_a_full_prompt_is_allowed_only_with_the_digest_of_what_was_shown(transport):
    sid = await start(transport)
    await waiting(transport, sid, full_prompt())
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert public["pendingApproval"]["input"] == FULL_INPUT and public["pendingApproval"]["full"] is True
    for index, shown in enumerate((None, "e" * 64)):
        with pytest.raises(ApiProblem, match="terminal") as refused:
            await service.submit_command(ALICE, None, sid, approve(requestId=f"bad-{index}", shownDigest=shown))
        assert refused.value.error.code == "LOCAL_SESSION_UNSUPPORTED"
    receipt = await service.submit_command(ALICE, None, sid, approve(shownDigest=DIGEST))
    assert receipt.decision == "allow"
    # The digest is the backend's check alone: the Mac's Command is deny_unknown_fields.
    assert "shownDigest" not in receipt.model_dump(by_alias=True)
    stored = await repo.get_command(OWNER, sid, "approve-1")
    assert "shownDigest" not in stored and stored["approvalShownDigest"] == DIGEST
    assert "shownDigest" not in (await service.get_command(ALICE, None, sid, "approve-1")).model_dump(by_alias=True)
    await relay(transport).dispatch_session(OWNER, sid)
    command = transport[3][-1][1]["command"]
    assert command["kind"] == "approve" and command["inputDigest"] == DIGEST
    assert not {"shownDigest", "approvalShownDigest"} & set(command)
    assert (await repo.get_command(OWNER, sid, "approve-1"))["state"] == "dispatched"


@pytest.mark.asyncio
async def test_a_shown_digest_never_unlocks_a_prompt_that_is_not_full(transport):
    sid = await start(transport)
    await waiting(transport, sid, full_prompt(full=False))
    with pytest.raises(ApiProblem, match="terminal"):
        await service.submit_command(ALICE, None, sid, approve(shownDigest=DIGEST))
    # A deny never needs it.
    assert (await service.submit_command(ALICE, None, sid, approve(decision="deny"))).decision == "deny"


@pytest.mark.asyncio
async def test_a_full_flag_without_the_call_stays_deny_only(transport):
    sid = await start(transport)
    for index, empty in enumerate(({"input": None}, {"input": {}})):
        prompt = full_prompt(**empty)
        if empty["input"] is None:
            del prompt["tool"]["input"]["input"]
        await waiting(transport, sid, prompt, seq=index)
        assert (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pendingApproval"]["full"] is False
        with pytest.raises(ApiProblem, match="terminal"):
            await service.submit_command(ALICE, None, sid, approve(requestId=f"bad-{index}", shownDigest=DIGEST))


@pytest.mark.asyncio
async def test_dispatch_refuses_a_full_allow_stored_without_its_shown_digest(transport):
    sid = await start(transport)
    await waiting(transport, sid, full_prompt())
    await service.submit_command(ALICE, None, sid, approve(shownDigest=DIGEST))
    stored = await repo.get_command(OWNER, sid, "approve-1")
    stripped = {k: v for k, v in stored.items() if k != "approvalShownDigest"}
    await repo.replace_command(stored, {**stripped, "revision": "r-stripped"})
    await relay(transport).dispatch_session(OWNER, sid)
    assert transport[3] == []
    assert (await repo.get_command(OWNER, sid, "approve-1"))["state"] == "rejected"


def test_a_shown_digest_belongs_to_approve_only():
    with pytest.raises(ValueError):
        service.LocalSessionCommand(requestId="r", expectedGeneration="g", kind="send", text="x", shownDigest=DIGEST)
    assert "shownDigest" not in approve().model_dump(by_alias=True)


def test_a_pending_approval_carries_its_input():
    event = service.LocalSessionEventBody.model_validate
    found = pending_approval([event(full_prompt())])
    assert found["input"] == FULL_INPUT and found["full"] is True
    # An older Mac's prompt keeps its wire: no input, not full.
    legacy = pending_approval([event(APPROVAL)])
    assert "input" not in legacy and legacy["full"] is False


@pytest.mark.asyncio
async def test_the_prompt_closes_when_the_session_stops_waiting(transport):
    sid = await start(transport)
    await waiting(transport, sid)
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="busy"))
    assert "pendingApproval" not in await repo.get_session(OWNER, sid)
    assert "pendingApproval" not in (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    with pytest.raises(ApiProblem, match="waiting"):
        await service.submit_command(ALICE, None, sid, approve())


def test_approval_commands_are_shaped_and_sends_stay_byte_identical():
    wire = send().model_dump(by_alias=True)
    assert set(wire) == {"requestId", "expectedGeneration", "kind", "text", "expectedTurnId"}
    with pytest.raises(ValueError):
        approve(inputDigest=None)
    with pytest.raises(ValueError):
        approve(decision="deny", mode="plan")
    with pytest.raises(ValueError):
        approve(text="only a deny explains itself")
    with pytest.raises(ValueError):
        service.LocalSessionCommand(requestId="r", expectedGeneration="g", kind="send", text="x", approvalId="appr-1")
    assert approve(decision="deny", text="no").text == "no"


def test_a_later_prompt_replaces_an_earlier_one_in_a_batch():
    event = service.LocalSessionEventBody.model_validate
    assert pending_approval([event({"kind": "text", "role": "assistant", "text": "hi"})]) is _NO_PROMPT
    answerable = pending_approval([event(APPROVAL)])
    assert answerable["approvalId"] == APPROVAL_ID
    # An untagged prompt (an older desktop, a question) after it is terminal-only.
    untagged = {k: v for k, v in APPROVAL.items() if k not in {"itemId", "tool"}}
    assert pending_approval([event(APPROVAL), event(untagged)]) is None
    # A malformed tag never becomes an answerable prompt.
    bad = {**APPROVAL, "tool": {**APPROVAL["tool"], "input": {"inputDigest": "nope"}}}
    assert pending_approval([event(bad)]) is None


@pytest.mark.asyncio
async def test_an_agent_never_answers_a_permission_prompt(transport, monkeypatch):
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    room = relay_tests.ROOM
    sid = await start(transport)
    await waiting(transport, sid)
    from core.repositories import messaging_repo

    await messaging_repo._transact(
        [
            {
                "Put": {
                    "TableName": messaging_repo._table().name,
                    "Item": {"pk": f"ROOM#{room}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent},
                }
            }
        ]
    )
    # Even woken by the owner's own message, the owner's agent cannot answer for them.
    caller = grants.verify(grants.mint(OWNER, room, agent, "44444444-4444-4444-8444-444444444444", ALICE.user_id))
    with pytest.raises(ApiProblem, match="person"):
        await service.submit_command(caller, room, sid, approve())


@pytest.mark.asyncio
async def test_a_restarted_process_starts_with_teammate_control_off(transport):
    sid = await start(transport)
    await service.set_team_control(ALICE, sid, True)
    # A new generation arrives without an Ended frame for the old one (a crash, a restart).
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await service.get_session(BOB, sid)).team_control is False


@pytest.mark.asyncio
async def test_teammate_control_never_extends_to_pausing_sharing(transport):
    sid = await start(transport)
    await service.set_team_control(ALICE, sid, True)
    with pytest.raises(ApiProblem, match="owner"):
        await service.set_sharing(BOB, sid, False)
    assert (await service.get_session(BOB, sid)).shared is True


async def test_model_and_mode_are_read_only_facts_from_a_desktop_that_was_told_it_may_send_them(transport):
    await service.set_enrollment(
        ALICE, service.EnrollmentRequest(deviceId=relay_tests.MAC, cliDeviceId=relay_tests.CLI, enabled=True)
    )
    relay_tests.connect(transport)
    registered = await relay(transport).handle_message("conn-1", registration())
    assert "sessionStats" in registered["features"]
    sid = registered["sessionId"]
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert "model" not in public and "permissionMode" not in public
    stats = {
        "model": "claude-opus-5-5",
        "permissionMode": "acceptEdits",
        "usage": {"inputTokens": 5, "outputTokens": 7},
    }
    await relay(transport).handle_message("conn-1", heartbeat(sid, stats=stats))
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert public["model"] == "claude-opus-5-5" and public["permissionMode"] == "acceptEdits"
    # A heartbeat that says nothing about stats leaves them as they were.
    await relay(transport).handle_message("conn-1", heartbeat(sid))
    assert (await service.get_session(BOB, sid)).permission_mode == "acceptEdits"
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", heartbeat(sid, stats={"model": "bad model\n"}))
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", heartbeat(sid, stats={"usage": {"inputTokens": 10**40}}))


async def _events(transport, sid, count, after=0):
    batch = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": f"batch-{after}",
        "afterSeq": after,
        "events": [
            {"kind": "tool", "role": "assistant", "text": f"line {after + i}", "truncated": False} for i in range(count)
        ],
    }
    await relay(transport).handle_message("conn-1", batch)


@pytest.mark.asyncio
async def test_the_token_meter_is_the_owners_until_the_owner_shares_it(transport):
    sid = await start(transport)
    usage = {"inputTokens": 1200, "outputTokens": 340, "cacheReadTokens": 90000, "cacheWriteTokens": 800}
    await relay(transport).handle_message("conn-1", heartbeat(sid, stats={"model": "claude-opus-5-5", "usage": usage}))
    assert (await service.get_session(ALICE, sid)).model_dump(by_alias=True)["usage"] == usage
    assert "usage" not in (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert all(s.usage is None for s in (await service.list_workspace_sessions(BOB)).sessions)
    assert (await service.list_workspace_sessions(ALICE)).sessions[0].usage is not None
    with pytest.raises(ApiProblem, match="owner"):
        await service.set_usage_sharing(BOB, sid, True)
    assert (await service.set_usage_sharing(ALICE, sid, True)).share_usage is True
    assert (await service.get_session(BOB, sid)).usage.output_tokens == 340
    await service.set_usage_sharing(ALICE, sid, False)
    assert (await service.get_session(BOB, sid)).usage is None


# --- Ask to join (owner ruling 2026-09-28) ---------------------------------------------------

CAROL = AuthContext(user_id="user_carol", org_id=OWNER)
NAMES = {"user_bob": "Bob", "user_carol": "Carol"}


async def _name(user_id):
    return NAMES.get(user_id, user_id)


async def ask(auth, sid, pushes=None):
    async def push(user_id, title, body, data=None):
        if pushes is not None:
            pushes.append((user_id, title, body, data))

    with (
        patch("core.services.push_service.send_push_to_user", push),
        patch("core.services.messaging.human_display_name", _name),
    ):
        return await seed_ask(auth, sid)


async def seed_ask(auth, sid):
    """Owner ruling 2026-10-26: only an "Anyone with the link" viewer asks to join. This files the
    pending request ``session_links.ask_to_join`` writes, without its outsider label (so Allow
    needs no Clerk call), and says it as that ask does."""

    def change(row):
        if (row.get("joinRequests") or {}).get(auth.user_id, {}).get("state") == "pending":
            return None
        return {**row, "joinRequests": {**(row.get("joinRequests") or {}), auth.user_id: service._new_request()}}

    row, audit = await service._change_session(auth, sid, change, "join_requested", None)
    if audit is not None:
        await service._notify_join_request(row, auth.user_id)
    return service._session_public(row, auth.user_id)


async def decide(auth, sid, user_id, decision, pushes, live=None):
    async def push(user_id, title, body, data=None):
        pushes.append((user_id, title, body, data))

    class Publisher:
        async def publish_user(self, owner, user, event):
            if live is not None and event["type"] == "workspace.session.attention":
                live.append((user, event["type"], event["payload"]))

    names = {**NAMES, ALICE.user_id: "Alice"}

    async def name(user_id):
        return names.get(user_id, user_id)

    with (
        patch("core.services.push_service.send_push_to_user", push),
        patch("core.services.messaging.human_display_name", name),
        patch("core.services.live_events.get_live_event_publisher", Publisher),
    ):
        return await service.decide_join(auth, sid, user_id, decision)


async def feed(auth):
    with patch("core.services.messaging.human_display_name", _name):
        return await activity.list_activity(auth, cursor=None, limit=30)


async def act(auth, item_id, action):
    with patch("core.services.messaging.human_display_name", _name):
        return await activity.run_action(auth, item_id, action, activity.ActivityActionRequest())


def joins(page):
    return [i for i in page.items if i.subtype == "join_request"]


@pytest.mark.asyncio
async def test_a_restart_tells_the_person_whose_ask_it_dropped_until_they_ask_again(transport):
    """ISOA-729: the session view remounts on a new generation, so only the server can say this."""
    sid = await start(transport)
    await ask(BOB, sid)
    # A new generation with no Ended frame for the old one (a crash, a restart).
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    bob = await service.get_session(BOB, sid)
    assert bob.join_request is None and bob.join_request_closed == "restarted"
    # Only the person whose ask was dropped reads it.
    assert (await service.get_session(CAROL, sid)).join_request_closed is None
    assert (await service.get_session(ALICE, sid)).join_request_closed is None
    assert "joinRequestClosed" not in (await service.get_session(CAROL, sid)).model_dump(by_alias=True)
    with patch.object(service, "JOIN_CLOSED_TTL_S", 0):  # bounded: it stops being said
        assert (await service.get_session(BOB, sid)).join_request_closed is None


@pytest.mark.asyncio
async def test_an_end_then_a_resume_also_tells_the_person_whose_ask_was_dropped(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await relay(transport).handle_message(
        "conn-1", {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1"}
    )
    assert (await service.get_session(BOB, sid)).join_request_closed is None  # ended: nothing to ask for
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await service.get_session(BOB, sid)).join_request_closed == "restarted"


@pytest.mark.asyncio
async def test_an_ask_to_join_tells_only_the_owner(transport):
    sid = await start(transport)
    pushes = []
    first = await ask(BOB, sid, pushes)
    assert first.join_request == "pending" and first.allowed_senders == []
    assert (await ask(BOB, sid, pushes)).join_request == "pending"  # a second tap adds nothing
    assert [(p[0], p[1]) for p in pushes] == [(ALICE.user_id, "Bob wants to join a session")]
    # The request state is the reader's own; who is waiting is the owner's alone.
    assert (await service.get_session(ALICE, sid)).join_request is None
    assert (await service.get_session(ALICE, sid)).pending_joins == [BOB.user_id]
    assert "pendingJoins" not in (await service.get_session(BOB, sid)).model_dump(by_alias=True)
    assert "pendingJoins" not in (await service.get_session(CAROL, sid)).model_dump(by_alias=True)
    assert "joinRequest" not in (await service.get_session(CAROL, sid)).model_dump(by_alias=True)
    live = [json.loads(h["events"][0]) for h in transport[4]]
    assert [e["payload"]["state"] for e in live if e["type"] == "workspace.session.attention"] == ["join_request"]
    assert any(e["type"] == "workspace.sessions.changed" for e in live)

    page = await feed(ALICE)
    item = joins(page)[0]
    assert page.items[0] is item and item.kind == "session" and item.state == "open"
    assert item.summary.title == "Bob wants to join a session"
    assert [(a.id, a.label, a.navigate) for a in item.actions] == [
        ("allow", "Allow", None),
        ("decline", "Decline", None),
    ]
    assert item.id.startswith(f"join:{sid}:{BOB.user_id}:")
    assert page.attention_count == 1
    assert joins(await feed(BOB)) == []
    assert await actions(sid) == [("join_requested", BOB.user_id)]


@pytest.mark.asyncio
async def test_members_never_ask_teammates_control_is_their_only_gate(transport):
    """Owner ruling 2026-10-26: trust = membership. Off: a member watches and cannot ask. On: a
    member types with no ask and no Allow."""
    sid = await start(transport)
    with pytest.raises(ApiProblem, match="teammates control"):
        await service.request_join(BOB, sid)
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, send("send-off"))
    assert (await service.get_session(ALICE, sid)).pending_joins is None
    assert joins(await feed(ALICE)) == []
    await service.set_team_control(ALICE, sid, True)
    assert (await service.submit_command(BOB, None, sid, send("send-on"))).actor_user_id == BOB.user_id
    assert (await service.get_session(ALICE, sid)).pending_joins is None


@pytest.mark.asyncio
async def test_turning_on_team_control_answers_an_old_member_ask_and_keeps_a_link_viewers(transport):
    sid = await start(transport)
    await seed_ask(BOB, sid)  # a member's ask left by a client from before the ruling

    def outsider(row):
        ask = {**service._new_request(), "outsider": True, "name": "Carol (carol@x.com)"}
        return {**row, "joinRequests": {**row["joinRequests"], CAROL.user_id: ask}}

    await service._change_session(CAROL, sid, outsider, "join_requested", None)
    assert (await service.get_session(ALICE, sid)).pending_joins == sorted([BOB.user_id, CAROL.user_id])
    await service.set_team_control(ALICE, sid, True)
    assert (await service.get_session(ALICE, sid)).pending_joins == [CAROL.user_id]
    assert (await service.get_session(BOB, sid)).join_request is None


@pytest.mark.asyncio
async def test_allow_lets_exactly_that_person_send(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await ask(CAROL, sid)
    with pytest.raises(ApiProblem, match="owner"):
        await service.decide_join(CAROL, sid, BOB.user_id, "allow")
    assert (await service.decide_join(ALICE, sid, BOB.user_id, "allow")).allowed_senders == [BOB.user_id]
    assert (await service.decide_join(ALICE, sid, BOB.user_id, "allow")).allowed_senders == [BOB.user_id]
    bob = await service.get_session(BOB, sid)
    assert bob.join_request is None and bob.allowed_senders == [BOB.user_id] and bob.team_control is False

    assert (await service.submit_command(BOB, None, sid, send())).actor_user_id == BOB.user_id
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(CAROL, None, sid, send("send-carol"))
    assert await actions(sid) == [
        ("join_requested", BOB.user_id),
        ("join_requested", CAROL.user_id),
        ("join_allowed", ALICE.user_id),
        ("send", BOB.user_id),
    ]
    assert (await service.list_audit(BOB, sid)).entries[2].subject_user_id == BOB.user_id
    # Dispatch re-checks the sender, who is still allowed.
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "send-1"))["state"] != "rejected"


@pytest.mark.asyncio
async def test_a_person_let_in_cannot_stop_the_owners_run(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    interrupt = service.LocalSessionCommand(requestId="stop-1", expectedGeneration="generation-1", kind="interrupt")
    with pytest.raises(ApiProblem, match="stop this run"):
        await service.submit_command(BOB, None, sid, interrupt)


@pytest.mark.asyncio
async def test_only_the_owner_ends_a_session_and_only_one_with_its_own_process(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    await service.set_team_control(ALICE, sid, True)
    row = await repo.get_session(OWNER, sid)
    end = service.LocalSessionCommand(requestId="end-1", expectedGeneration=row["generation"], kind="end")
    with pytest.raises(ApiProblem, match="owner can end"):
        service.require_command_scope(BOB, row, "end")  # not even under team control
    service.require_command_scope(ALICE, row, "end")
    # Ending needs no send capability, so an unverified session can still be ended.
    service._require_control({**row, "provider": "claude", "capabilitiesVerified": False}, end)
    with pytest.raises(ApiProblem, match="on its Mac"):
        service._require_control({**row, "provider": "codex", "terminal": "attach"}, end)
    service._require_control({**row, "provider": "codex", "terminal": "held"}, end)  # Lightbulb holds it
    with pytest.raises(ApiProblem, match="on its Mac"):  # Claude's daemon owns a background session
        service._require_control({**row, "provider": "claude", "kind": "background", "terminal": "attach"}, end)
    with pytest.raises(ValueError):
        service.LocalSessionCommand(requestId="end-2", expectedGeneration="g", kind="end", text="bye")


@pytest.mark.asyncio
async def test_a_queued_stop_from_a_person_let_in_is_refused_at_delivery(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    await service.set_team_control(ALICE, sid, True)
    row = await repo.get_session(OWNER, sid)
    service.require_command_scope(BOB, row, "interrupt")  # team control on: allowed
    await service.set_team_control(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="stop this run"):
        service.require_command_scope(BOB, await repo.get_session(OWNER, sid), "interrupt")
    service.require_command_scope(BOB, await repo.get_session(OWNER, sid), "send")  # follow-ups still fine


@pytest.mark.asyncio
async def test_an_activity_answer_never_lands_on_a_newer_request(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    old = joins(await feed(ALICE))[0]
    await service.decide_join(ALICE, sid, BOB.user_id, "decline")  # answered elsewhere
    await ask(BOB, sid)  # Bob asks again: a new request
    with pytest.raises(ApiProblem, match="already answered"):
        await service.decide_join(ALICE, sid, BOB.user_id, "allow", request_id=old.id.rsplit(":", 1)[1])
    assert (await service.get_session(BOB, sid)).join_request == "pending"


@pytest.mark.asyncio
async def test_an_agent_never_rides_a_persons_allow(transport, db, monkeypatch):
    from core.services import local_session_grants as grants
    from tests.unit.services.test_local_sessions import OTHER_ROOM, ROOM

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": "MEMBER#agent#friday", "kind": "agent", "id": "friday"})
    sid = await start(transport)
    await ask(BOB, sid)
    await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    caller = grants.verify(grants.mint(OWNER, ROOM, "friday", OTHER_ROOM, BOB.user_id))
    with pytest.raises(ApiProblem, match="Only a person"):
        await service.submit_command(caller, ROOM, sid, send("agent-1"))


@pytest.mark.asyncio
async def test_decline_ask_again_allow_from_activity_and_remove(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    assert (await service.decide_join(ALICE, sid, BOB.user_id, "decline")).allowed_senders == []
    assert (await service.get_session(BOB, sid)).join_request == "declined"
    await service.decide_join(ALICE, sid, BOB.user_id, "decline")  # a repeat is a no-op
    with pytest.raises(ApiProblem, match="already answered"):
        await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, send())
    assert [i.state for i in joins(await feed(ALICE))] == ["resolved"]
    assert (await ask(BOB, sid)).join_request == "pending"  # a declined person may ask again
    items = joins(await feed(ALICE))
    assert [i.state for i in items] == ["open", "resolved"]

    with pytest.raises(ApiProblem):
        await act(BOB, items[0].id, "allow")  # only the owner's feed holds it
    done = await act(ALICE, items[0].id, "allow")
    assert done.state == "resolved" and done.actions == []
    assert (await act(ALICE, items[0].id, "allow")).state == "resolved"  # a retried tap
    assert (await service.get_session(BOB, sid)).allowed_senders == [BOB.user_id]

    with pytest.raises(ApiProblem, match="owner"):
        await service.remove_sender(BOB, sid, BOB.user_id)
    assert (await service.remove_sender(ALICE, sid, BOB.user_id)).allowed_senders == []
    assert (await service.remove_sender(ALICE, sid, BOB.user_id)).allowed_senders == []
    with pytest.raises(ApiProblem, match="owner"):
        await service.submit_command(BOB, None, sid, send("send-2"))
    assert [a for a, _ in await actions(sid)] == [
        "join_requested",
        "join_declined",
        "join_requested",
        "join_allowed",
        "sender_removed",
    ]


@pytest.mark.asyncio
async def test_a_relay_reconnect_keeps_the_allow_list_and_the_end_clears_it(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
        await ask(BOB, sid)
        await service.decide_join(ALICE, sid, BOB.user_id, "allow")
        await ask(CAROL, sid)
    # The desktop's relay reconnects and re-registers the same process (same generation).
    connect(transport, cid="conn-2", stamp="2000")
    with patch("time.time", return_value=10002):
        await relay(transport).handle_message("conn-2", registration())
        assert (await service.get_session(BOB, sid)).allowed_senders == [BOB.user_id]
        assert (await service.get_session(CAROL, sid)).join_request == "pending"
        await service.submit_command(BOB, None, sid, send())
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with patch("time.time", return_value=10006):
        await relay(transport).handle_message("conn-2", frame)
    row = await repo.get_session(OWNER, sid)
    assert "allowedSenders" not in row and "joinRequests" not in row
    assert (await service.get_session(CAROL, sid)).join_request is None
    with pytest.raises(ApiProblem, match="ended"):
        await ask(CAROL, sid)
    assert [i.state for i in joins(await feed(ALICE))] == ["resolved", "resolved"]


@pytest.mark.asyncio
async def test_join_routes(transport):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient

    from core.auth import get_current_user
    from routers import messaging

    sid = await start(transport)
    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda r, exc: JSONResponse(status_code=exc.status_code, content=exc.body()))
    base = f"/api/v1/messaging/sessions/{sid}"
    with (
        patch("core.services.push_service.send_push_to_user", _no_push),
        patch("core.services.messaging.human_display_name", _name),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_current_user] = lambda: BOB
            # Members never ask (owner ruling 2026-10-26); a link viewer's ask is answered here.
            assert (await client.post(f"{base}/join-requests")).status_code == 403
            await seed_ask(BOB, sid)
            decide = f"{base}/join-requests/{BOB.user_id}"
            assert (await client.post(decide, json={"decision": "allow"})).status_code == 403
            app.dependency_overrides[get_current_user] = lambda: ALICE
            assert (await client.post(decide, json={"decision": "maybe"})).status_code == 422
            allowed = await client.post(decide, json={"decision": "allow"})
            assert allowed.status_code == 200 and allowed.json()["allowedSenders"] == [BOB.user_id]
            removed = await client.delete(f"{base}/senders/{BOB.user_id}")
            assert removed.status_code == 200 and removed.json()["allowedSenders"] == []


async def _no_push(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_a_new_process_drops_the_allow_list_like_team_control(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    await service.decide_join(ALICE, sid, BOB.user_id, "allow")
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await service.get_session(BOB, sid)).allowed_senders == []


@pytest.mark.asyncio
async def test_allow_tells_the_requester_once(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    pushes, live = [], []
    await decide(ALICE, sid, BOB.user_id, "allow", pushes, live)
    await decide(ALICE, sid, BOB.user_id, "allow", pushes, live)  # a repeat is a no-op: no second push
    assert pushes == [
        (
            BOB.user_id,
            f"Alice let you into {sid[:8]}",
            "You can send follow-ups and answer its prompts.",
            {"source": "session", "sessionId": sid},
        )
    ]
    assert [(user, kind, payload["state"]) for user, kind, payload in live] == [
        (BOB.user_id, "workspace.session.attention", "join_allowed")
    ]
    assert live[0][2]["push"]["title"] == pushes[0][1]


@pytest.mark.asyncio
async def test_decline_tells_the_requester_once(transport):
    sid = await start(transport)
    await ask(BOB, sid)
    pushes = []
    await decide(ALICE, sid, BOB.user_id, "decline", pushes)
    await decide(ALICE, sid, BOB.user_id, "decline", pushes)
    assert pushes == [(BOB.user_id, "Alice declined your request", sid[:8], {"source": "session", "sessionId": sid})]


@pytest.mark.asyncio
async def test_sharing_and_join_events_land_on_the_person_who_acted(transport):
    """Growth funnel: a link made, a link opened, an answer to an ask. Each lands on
    the person who did it, names the session's author, and carries no transcript text."""
    sid = await start(transport)
    await _events(transport, sid, 1)
    with patch("core.services.local_sessions.capture_product_event", create=True) as capture:
        link = await service.put_link(ALICE, sid, "anyone")
        token = link.url.split("#", 1)[1]
        await session_links.open_page(BOB, token)
        await session_links.open_page(BOB, token, refresh=True)  # the page's poll is not another open
        await ask(BOB, sid)
        await decide(ALICE, sid, BOB.user_id, "allow", pushes=[])
    assert [(c.kwargs["event"], c.kwargs["distinct_id"]) for c in capture.call_args_list] == [
        ("share_link_created", ALICE.user_id),
        ("share_link_opened", BOB.user_id),
        ("session_join_decided", ALICE.user_id),
    ]
    for call in capture.call_args_list:
        props = call.kwargs["properties"]
        assert props["workspace_id"] == OWNER and props["session_id"] == sid
        assert (
            props["author_user_id"] == ALICE.user_id and props["provider"] == "codex"
        )  # the harness registers a Codex session
        assert "line 0" not in str(call.kwargs)
    assert capture.call_args_list[0].kwargs["properties"]["audience"] == "anyone"
    assert capture.call_args_list[-1].kwargs["properties"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_pick_up_request_shows_and_is_answered_in_activity(transport):
    from tests.unit.services.test_session_pickup import claude_session

    sid = await claude_session(transport)

    async def no_push(*args, **kwargs):
        return None

    with (
        patch("core.services.push_service.send_push_to_user", no_push),
        patch("core.services.messaging.human_display_name", _name),
    ):
        await service.request_pickup_access(BOB, sid)
    rid = (await repo.get_session(OWNER, sid))["pickupRequests"][BOB.user_id]["requestId"]
    [item] = joins(await feed(ALICE))
    assert item.id == f"pickup:{sid}:{BOB.user_id}:{rid}" and item.state == "open"
    assert item.summary.title == "Bob wants to pick up a session"
    assert item.summary.reason == (
        "Allow lets everyone in the workspace pick up this session on their Mac. It uploads the whole "
        "conversation and your uncommitted changes as they are. Nothing is redacted."
    )
    assert [a.id for a in item.actions] == ["allow", "decline"]
    decide = service.decide_pickup_request
    calls = []

    async def spy(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return await decide(*args, **kwargs)

    with patch.object(service, "decide_pickup_request", spy), patch.object(service, "decide_join") as join:
        done = await act(ALICE, item.id, "allow")
    assert calls == [((sid, BOB.user_id, "allow"), {"request_id": rid})] and not join.called
    assert done.id == item.id and done.state == "resolved" and done.actions == []
    assert (await repo.get_session(OWNER, sid))["pickupAllowed"] is True


@pytest.mark.asyncio
async def test_link_changes_log_who_and_which_client_and_never_the_link(caplog):
    """ISOA-732: a link change could be traced to a person (the audit row) but not to a client.
    The route says which, without the token or URL. PUT is the only route that changes a link
    since "Turn off link" went (#3946): narrowing it is a PUT too."""
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from core.auth import get_current_user
    from routers import messaging

    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: ALICE
    sid, secret = "8ad82a70-e3cd-5d26-b43a-9de9db3dd0c3", "https://x/s#do-not-log-this-token"
    links = [service.SessionLink(url=secret, audience=audience) for audience in ("anyone", "workspace")]
    headers = {"user-agent": "Mozilla/5.0 TestWebView", "origin": "https://thelightbulb.company"}
    with (
        patch.object(service, "put_link", AsyncMock(side_effect=links)),
        caplog.at_level("INFO", logger="routers.messaging"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/v1/messaging/sessions/{sid}/link"
            for audience in ("anyone", "workspace"):
                assert (await client.put(path, json={"audience": audience}, headers=headers)).status_code == 200
    records = [r for r in caplog.records if r.getMessage().startswith("session link ")]
    assert [(r.getMessage(), r.audience) for r in records] == [
        ("session link put", "anyone"),
        ("session link put", "workspace"),
    ]
    for record in records:
        assert record.actor_user_id == ALICE.user_id and record.session_id == sid
        assert record.user_agent == "Mozilla/5.0 TestWebView" and record.origin == "https://thelightbulb.company"
        assert "do-not-log" not in str(record.__dict__)


@pytest.mark.asyncio
async def test_a_session_that_takes_no_input_shows_no_answerable_prompt(transport):
    """Its every answer is refused, so a card with live buttons could only fail (prod 2026-10-19)."""
    sid = await start(transport)
    await waiting(transport, sid)
    row = await repo.get_session(OWNER, sid)
    assert service._session_public(row, ALICE.user_id).pending_approval is not None
    for blocked in ({"controlUnavailableReason": "runtime-unverified"}, {"capabilitiesVerified": False}):
        assert service._session_public({**row, **blocked}, ALICE.user_id).pending_approval is None
