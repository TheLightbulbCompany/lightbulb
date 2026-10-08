"""The Google Meet floor (setting="meeting"): an agent speaks when named, or when a question to the
room went unclaimed; a lone speaker never implies an answer, and a Jev outage wakes nobody."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import jev
from core.services import voice_calls as vc
from tests.unit.services.voice_stubs import jev_says

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


async def _meet(text, said, unclaimed=False, floor=None, engagements=None):
    decide = vc.jev.decide
    decide.return_value = said
    return await vc.route_live_line(
        owner_id="org_a",
        room_id="room-1",
        speaker_id="Priya + Dan",
        utterance=text,
        transcript="",
        humans=1,
        setting="meeting",
        unclaimed=unclaimed,
        floor=floor,
        engagements=engagements,
    )


def _nobody(got):
    return (
        got["agentId"] is None
        and got["agentIds"] == []
        and got["ops"]["requests"] == []
        and got["ops"]["social"] is None
    )


_REAL_ROOM_TEAM = vc._room_team  # captured before the autouse fixture mocks it


async def test_meeting_setting_disables_the_lone_speaker_fallback(env):
    """In Live a lone speaker's small talk or question wakes the CEO; in a meeting it wakes nobody."""
    assert _nobody(await _meet("hmm, let me share my screen", jev_says(kinds=["social"])))
    assert _nobody(await _meet("hmm", None))  # Jev down: silence
    assert _nobody(await _meet("what's our burn", jev_says(kinds=["request"], whos=["fin-1"])))  # even a sure ask
    env.assert_not_awaited()  # a Meet speaker label is a name, never a Clerk lookup


async def test_holding_the_floor_in_a_meeting_implies_nothing_without_evidence_the_speaker_is_talking_with_the_agent():
    """The Meet's one mixed voice keeps ``floor.mine`` true between people. An unnamed request is the agent's
    only when the worker's evidence says its speaker is talking with it. Chatter is never answered."""
    floor = {"agent": "fin-1", "mine": True, "speaker": "Priya + Dan"}
    assert _nobody(await _meet("ha, nice one", jev_says(kinds=["social"]), floor=floor))
    assert _nobody(await _meet("what's our burn", jev_says(kinds=["request"]), floor=floor))
    heard = [{"agentId": "fin-1", "state": "active", "expects": "nothing", "last": "delivered", "lastBy": ""}]
    got = await _meet("what's our burn", jev_says(kinds=["request"]), floor=floor, engagements=heard)
    assert got["agentIds"] == ["fin-1"] and got["ops"]["requests"][0]["by"] == "floor"
    assert _nobody(await _meet("ha, nice one", jev_says(kinds=["social"]), floor=floor, engagements=heard))


async def test_unclaimed_question_in_a_meeting_picks_at_most_one():
    got = await _meet("what's our burn?", jev_says(kinds=["people"]), unclaimed=True)
    assert got["agentIds"] == ["ceo-1"] and got["ops"]["requests"][0]["by"] == "chief"
    got = await _meet("what's our burn?", None, unclaimed=True)
    assert got["agentIds"] == ["ceo-1"]
    got = await _meet("what's our burn?", jev_says(kinds=["request"], whos=["fin-1"]), unclaimed=True)
    assert got["agentIds"] == ["fin-1"] and len(got["ops"]["requests"]) == 1


async def test_a_name_anywhere_still_addresses():
    said = jev_says(kinds=["request"], names=["addressed"])
    got = await _meet("so I think Finance should check the burn", said)
    assert got["agentIds"] == ["fin-1"] and got["ops"]["requests"][0]["by"] == "name"


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
    vc.jev.decide.return_value = jev_says(kinds=["social"])
    with (
        patch.object(vc, "_room_team", _REAL_ROOM_TEAM),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=members)),
        patch.object(vc.messaging.messaging_repo, "get_room", AsyncMock(return_value=meta)),
        patch.object(vc.messaging.messaging_repo, "get_member", get_member),
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
    assert _nobody(got)
    get_member.assert_awaited_once_with(room, "human", "user_inviter")
    assert vc.jev.decide.await_args.args[0]["latest"]["author"] == "Priya + Dan"
