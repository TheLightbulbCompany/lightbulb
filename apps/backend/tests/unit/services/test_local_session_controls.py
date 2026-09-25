"""Team control, remote approvals and the audit trail (owner ruling 2026-09-25).

Real Moto transactions through the relay; the desktop is the shared wire fixtures.
"""

from unittest.mock import patch

import pytest

from core.repositories import local_sessions_repo as repo
from core.services import local_sessions as service
from core.services.local_session_relay import _NO_PROMPT, pending_approval
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import fixture, heartbeat, registration, relay, start
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
async def test_a_prompt_the_approver_cannot_see_whole_is_deny_only(transport):
    sid = await start(transport)
    inexact = {**APPROVAL, "tool": {**APPROVAL["tool"], "input": {**APPROVAL["tool"]["input"], "exact": False}}}
    await waiting(transport, sid, inexact)
    with pytest.raises(ApiProblem, match="terminal"):
        await service.submit_command(ALICE, None, sid, approve())
    receipt = await service.submit_command(ALICE, None, sid, approve(decision="deny", text="not on main"))
    assert receipt.decision == "deny"
    assert (await actions(sid))[-1] == ("deny", ALICE.user_id)


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
