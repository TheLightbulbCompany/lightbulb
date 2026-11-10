"""Board row 3.29: the owner calls the business's own number from their saved mobile, types their PIN, and talks as
themselves (the orb's owner path), not as a caller. Caller ID alone is never proof."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.repositories import messaging_repo
from routers import voice, voice_phone
from tests.unit.routers import test_voice_phone as vp

W, _start = vp.W, vp._start
client, router_seat = vp.client, vp.router_seat  # the phone tests' moto table and Router seat, as fixtures here

MOBILE = "+14155550133"  # the caller number _start dials from
DM = "7f1d2c3b-0000-4000-8000-0000000000dd"
OWNER = AuthContext(user_id="user_owner", org_id="org_1")


@pytest.fixture
async def dm(client):
    """The member's DM with the assistant, seated as the orb's (owner_dm opens it)."""
    members = [{"kind": "human", "id": "user_owner", "displayName": None, "mentionHandle": None, "role": "owner",
                "listenMode": None},
               {"kind": "agent", "id": "agent_front", "displayName": "Lightbulb", "mentionHandle": "lightbulb",
                "role": "member", "listenMode": "active"}]  # fmt: skip
    await messaging_repo.create_room(
        owner_id="org_1", room_id=DM, client_room_id="dm", creator_user_id="user_owner", name=None, kind="dm",
        members=members, req_hash=messaging_repo.request_hash(None, "dm", DM),
    )  # fmt: skip
    with patch.object(voice_phone, "owner_dm", AsyncMock(return_value=DM)) as opened:
        yield opened


def _pin(client, call, pin):
    r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/owner-pin", json={"pin": pin}, headers=W)
    assert r.status_code == 200, r.text
    return r.json()


def _turn(client, call, n=0, text="What's on my calendar today?"):
    return client.post(f"/api/v1/internal/voice/calls/{call['callId']}/turn", json={"n": n, "text": text}, headers=W)


async def _set(mobile=MOBILE, pin="4821", auth=OWNER):
    return await voice.put_phone_pin(voice.PhonePinIn(mobile=mobile, pin=pin), auth)


async def test_the_pin_is_stored_hashed_and_never_shown_back(client):
    assert await voice.get_phone_pin(OWNER) == {"set": False, "mobile": ""}
    assert await _set(mobile="(415) 555-0133") == {"set": True, "mobile": MOBILE}
    assert await voice.get_phone_pin(OWNER) == {"set": True, "mobile": MOBILE}
    row = await messaging_repo._get("OWNER#org_1", "PHONEPIN#user_owner")
    assert "4821" not in str(row) and len(row["hash"]) == 64 and row["salt"]


async def test_no_pin_set_means_owner_mode_is_impossible(client, router_seat, dm):
    call = await _start(client)
    assert call["ownerPin"] is False
    assert _pin(client, call, "4821") == {"ok": False, "locked": False}
    assert (await messaging_repo.get_room(call["callId"]))["call"]["ownerMode"] is False
    dm.assert_not_awaited()


async def test_caller_id_without_the_pin_stays_a_caller(client, router_seat, dm):
    _, enq = router_seat
    await _set()
    call = await _start(client)
    assert call["ownerPin"] is True  # the voice may offer the keypad; it proves nothing
    out = _turn(client, call).json()
    msg = await messaging_repo.get_message_by_id(call["callId"], out["messageId"])
    assert msg["author"]["id"] == f"caller:{call['callId']}"  # the caller principal, in the call's room
    assert enq.await_args.args[1].ref["origin"] == "phone_contact"


async def test_wrong_pins_lock_the_call_and_then_the_right_one_is_refused(client, router_seat, dm):
    await _set()
    call = await _start(client)
    assert [_pin(client, call, "0000") for _ in range(3)] == [
        {"ok": False, "locked": False}, {"ok": False, "locked": False}, {"ok": False, "locked": True}]  # fmt: skip
    assert _pin(client, call, "4821") == {"ok": False, "locked": True}
    rec = (await messaging_repo.get_room(call["callId"]))["call"]
    assert rec["ownerMode"] is False
    assert "0000" not in str(rec) and "4821" not in str(rec)
    dm.assert_not_awaited()


async def test_concurrent_guesses_never_exceed_the_limits(client, router_seat, dm):
    """Every attempt reserves a try with a conditional update before the hash is checked: ten guesses at once on
    one call check at most CALL_TRIES PINs, and calls at once never check more than DAY_TRIES for the member."""
    await _set()
    checked = []
    real = voice_phone.phone_pin._hash

    def counting(pin, salt):
        checked.append(pin)
        return real(pin, salt)

    with patch.object(voice_phone.phone_pin, "_hash", counting):
        call = await _start(client)
        outs = await asyncio.gather(*(voice_phone.owner_pin(call["callId"], voice_phone.PinIn(pin=f"{i:04d}"))
                                      for i in range(10)))  # fmt: skip
        assert sum(o["ok"] for o in outs) == 0 and len(checked) == voice_phone.phone_pin.CALL_TRIES
        checked.clear()
        calls = [await _start(client, room=f"phone-{i}") for i in range(6)]
        await asyncio.gather(*(voice_phone.owner_pin(c["callId"], voice_phone.PinIn(pin=f"{j}{j}{j}{j}"))
                               for c in calls for j in range(3)))  # fmt: skip
    # the day's tries left after the first call's three, all wrong; the right PIN is refused once they are spent
    assert len(checked) == voice_phone.phone_pin.DAY_TRIES - voice_phone.phone_pin.CALL_TRIES
    last = await _start(client, room="phone-last")
    assert _pin(client, last, "4821")["ok"] is False


async def _owner_call(client):
    await _set()
    call = await _start(client)
    assert _pin(client, call, "4821")["ok"] is True
    return call


async def test_an_owner_mode_answer_to_a_workers_question_answers_it(client, router_seat, dm):
    """As the orb (voice_owner.turn): the line said right after the voice said a Worker's question goes through the
    card's own answer path, and wakes nobody; a message alone never answers a Question."""
    call = await _owner_call(client)
    ref = {"taskId": "t-1", "qid": "q-1"}
    await messaging_repo.create_message(
        room_id=DM, owner_id="org_1", idemp_pk=messaging_repo.owner_pk("org_1"), idemp_sk="q", req_hash="q",
        author={"kind": "agent", "id": "agent_front", "displayName": "Lightbulb"}, text="Which day works?",
        mentions=[], reply_to_message_id=None, thread_root_message_id=None, run_id=None, dispatch_targets=[],
        source_agent_id=None, client_message_id=None, author_member_check=False,
        attrs={"kind": "question", "taskRefs": [ref]},
    )  # fmt: skip
    sent = AsyncMock(return_value=type("M", (), {"message_id": "m-a"})())
    from routers import questions

    with (
        patch.object(questions, "submit_answer", AsyncMock()) as answer,
        patch.object(voice_phone.messaging, "create_human_message", sent),
    ):
        r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/turn",
                        json={"n": 1, "text": "Tuesday", "answers": ref}, headers=W)  # fmt: skip
    assert r.json() == {"messageId": "m-a", "answered": "q-1"}
    owner, task, qid, member, body = answer.await_args.args
    assert (owner, task, qid, member, body.text) == ("org_1", "t-1", "q-1", "user_owner", "Tuesday")
    assert sent.await_args.kwargs == {"via": "voice", "wake": False}


async def test_a_turn_that_finds_the_member_removed_carries_the_end_of_owner_mode(client, router_seat, dm):
    """Removed, and the next thing the worker sends is a line (no poll in between): `/turn` itself says owner mode
    ended, with the call room's cursor just before this line, so the caller's replies are not skipped."""
    call = await _owner_call(client)
    meta = await messaging_repo.get_room(DM)
    await messaging_repo.remove_member(meta=meta, kind="human", member_id="user_owner", new_delivery_key="k2",
                                       live_events=[])  # fmt: skip
    out = _turn(client, call, n=2).json()
    msg = await messaging_repo.get_message_by_id(call["callId"], out["messageId"])
    assert out["ownerModeEnded"] is True and out["after"] == int(msg["messageSeq"]) - 1
    assert "ownerModeEnded" not in _turn(client, call, n=3).json()  # said once


async def test_removing_the_member_mid_call_ends_owner_mode(client, router_seat, dm):
    call = await _owner_call(client)
    meta = await messaging_repo.get_room(DM)
    await messaging_repo.remove_member(meta=meta, kind="human", member_id="user_owner", new_delivery_key="k2",
                                       live_events=[])  # fmt: skip
    sent = AsyncMock()
    with (
        patch.object(voice_phone, "agent_lines", AsyncMock(return_value={"messages": [{"id": "x"}], "after": 9})),
        patch.object(voice_phone.messaging, "create_human_message", sent),
    ):
        r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/events", json={"after": 5}, headers=W)
        out = r.json()
        assert out["ownerModeEnded"] is True and out["messages"] == []
        turn = _turn(client, call, n=2).json()
    sent.assert_not_awaited()  # the line is a caller's again, in the call room
    msg = await messaging_repo.get_message_by_id(call["callId"], turn["messageId"])
    assert msg["author"]["id"] == f"caller:{call['callId']}"
    assert (await messaging_repo.get_room(call["callId"]))["call"]["ownerMode"] is False


async def test_a_day_of_wrong_pins_locks_the_member_across_calls(client, router_seat, dm):
    await _set()
    for room in ("phone-a", "phone-b"):
        call = await _start(client, room=room)
        for _ in range(3):
            _pin(client, call, "1111")  # 6 wrong tries over two calls: past DAY_TRIES
    call = await _start(client, room="phone-c")
    assert _pin(client, call, "4821") == {"ok": False, "locked": False}
    dm.assert_not_awaited()


async def test_a_different_number_with_the_right_pin_is_refused(client, router_seat, dm):
    await _set()
    call = await _start(client, caller="+14155550999")
    assert call["ownerPin"] is False
    assert _pin(client, call, "4821")["ok"] is False
    assert (await messaging_repo.get_room(call["callId"]))["call"]["ownerMode"] is False


async def test_the_right_pin_flips_the_call_and_the_next_line_is_the_members_own(client, router_seat, dm):
    _, enq = router_seat
    await _set()
    call = await _start(client)
    assert _pin(client, call, "4821") == {"ok": True, "after": 0}
    rec = (await messaging_repo.get_room(call["callId"]))["call"]
    assert (rec["ownerMode"], rec["ownerModeUserId"], rec["dmRoomId"]) == (True, "user_owner", DM)
    dm.assert_awaited_once_with("org_1", "user_owner")
    sent = AsyncMock(return_value=type("M", (), {"message_id": "m-owner"})())
    with patch.object(voice_phone.messaging, "create_human_message", sent):
        assert _turn(client, call, n=3).json() == {"messageId": "m-owner"}
    auth, room, request = sent.await_args.args
    # the orb's owner path (voice_owner.turn): the member's message in their DM, which wakes the DM's Router
    assert (auth.user_id, auth.org_id, room) == ("user_owner", "org_1", DM)
    assert request.text == "What's on my calendar today?" and sent.await_args.kwargs == {"via": "voice"}
    enq.assert_not_awaited()  # no caller trigger
    with patch.object(voice_phone, "agent_lines", AsyncMock(return_value={"messages": [], "after": 7})) as lines:
        r = client.post(f"/api/v1/internal/voice/calls/{call['callId']}/events", json={"after": 5}, headers=W)
    assert r.json() == {"messages": [], "after": 7} and lines.await_args.args == (DM, 5)
