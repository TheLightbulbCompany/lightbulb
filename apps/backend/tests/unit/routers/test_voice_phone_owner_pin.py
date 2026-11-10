"""Board row 3.29: the owner calls the business's own number from their saved mobile, types their PIN, and talks as
themselves (the orb's owner path), not as a caller. Caller ID alone is never proof."""

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
def dm():
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
    assert rec["ownerMode"] is False and rec["pinTries"] == 3
    assert "0000" not in str(rec) and "4821" not in str(rec)
    dm.assert_not_awaited()


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
