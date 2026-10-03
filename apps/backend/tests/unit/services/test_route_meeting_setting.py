"""The Google Meet floor (setting="meeting"): an agent speaks when named, or when a question to the
room went unclaimed; a lone speaker never implies an answer, and a Jev outage wakes nobody."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import jev
from core.services import voice_calls as vc

MEMBERS = [
    {"kind": "agent", "id": "ceo-1", "displayName": "Ada"},
    {"kind": "agent", "id": "fin-1", "displayName": "Finance"},
]


@pytest.fixture(autouse=True)
def env():
    with (
        patch.object(vc, "_room_team", AsyncMock(return_value=({"name": "meet", "kind": "channel"}, []))),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(vc.messaging, "human_display_name", AsyncMock()) as lookup,
        patch.object(vc.openclaw_work, "_fallback_ceo_id", AsyncMock(return_value="ceo-1")),
        patch.object(vc.jev, "decide", AsyncMock(return_value=None)),  # the addressing stage's call
    ):
        yield lookup


async def _meet(text, decision, unclaimed=False):
    decide = AsyncMock(return_value=decision)
    with patch.object(vc.jev, "_decide_for_message", decide):
        got = await vc.route_live_line(
            owner_id="org_a",
            room_id="room-1",
            speaker_id="Priya + Dan",
            utterance=text,
            transcript="",
            humans=1,
            setting="meeting",
            unclaimed=unclaimed,
        )
    return got, decide


NOBODY = {"agentId": None, "agentIds": []}
_REAL_ROOM_TEAM = vc._room_team  # captured before the autouse fixture mocks it


async def test_meeting_setting_disables_the_lone_speaker_fallback(env):
    got, decide = await _meet("hmm, let me share my screen", {"who": "none"})
    assert got == NOBODY  # in Live a lone speaker's "none" would wake the CEO
    assert decide.await_args.kwargs["setting"] == "meeting"
    assert (await _meet("hmm", {"fallback": True}))[0] == NOBODY  # Jev down: silence
    assert (await _meet("what's our burn", {"who": "none", "forced": "question"}))[0] == NOBODY
    env.assert_not_awaited()  # a Meet speaker label is a name, never a Clerk lookup


async def test_unclaimed_question_in_a_meeting_picks_at_most_one():
    got, _ = await _meet("what's our burn?", {"who": "none"}, unclaimed=True)
    assert got == {"agentId": "ceo-1", "agentIds": ["ceo-1"]}
    got, _ = await _meet("what's our burn?", {"fallback": True}, unclaimed=True)
    assert got == {"agentId": "ceo-1", "agentIds": ["ceo-1"]}
    got, _ = await _meet("what's our burn?", {"who": "fin-1"}, unclaimed=True)
    assert got == {"agentId": "fin-1", "agentIds": ["fin-1"]}


async def test_a_name_anywhere_still_addresses():
    got, decide = await _meet("so I think Finance should check the burn", {"who": "none"})
    assert got == {"agentId": "fin-1", "agentIds": ["fin-1"]}
    decide.assert_not_awaited()


async def test_jev_is_told_it_is_a_meeting():
    decide = AsyncMock(return_value={"who": {"choice": "none", "probabilities": {}}})
    message = {"author": {"kind": "human", "id": "Someone"}, "text": "hi", "dispatchTargets": []}
    with patch.object(jev, "decide", decide):
        await jev._decide_for_message(
            {"ownerId": "org_a"}, message, message, MEMBERS, [], to_people=False, placement=False, setting="meeting"
        )
        assert decide.await_args.args[0]["room"]["setting"] == jev.MEETING_CONTEXT
        await jev._decide_for_message({"ownerId": "org_a"}, message, message, MEMBERS, [], placement=False)
        assert "setting" not in decide.await_args.args[0]["room"]


async def test_a_meeting_line_authorizes_as_the_inviting_owner_and_keeps_the_label():
    """The real _room_team: a Meet label is no member, so the line acts as the room's owner member."""
    room = "11111111-1111-4111-8111-111111111111"
    members = [*MEMBERS, {"kind": "human", "id": "user_inviter", "role": "owner"}]
    meta = {"roomId": room, "ownerId": "org_a", "kind": "channel", "name": "meet"}
    get_member = AsyncMock(return_value={"kind": "human", "id": "user_inviter", "role": "owner"})
    decide = AsyncMock(return_value={"who": "none"})
    with (
        patch.object(vc, "_room_team", _REAL_ROOM_TEAM),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=members)),
        patch.object(vc.messaging.messaging_repo, "get_room", AsyncMock(return_value=meta)),
        patch.object(vc.messaging.messaging_repo, "get_member", get_member),
        patch.object(vc.jev, "_decide_for_message", decide),
    ):
        got = await vc.route_live_line(
            owner_id="org_a",
            room_id=room,
            speaker_id="Priya + Dan",
            utterance="hmm",
            transcript="",
            humans=2,
            setting="meeting",
        )
    assert got == NOBODY
    get_member.assert_awaited_once_with(room, "human", "user_inviter")
    assert decide.await_args.args[1]["author"] == {"kind": "human", "id": "Priya + Dan", "displayName": "Priya + Dan"}
