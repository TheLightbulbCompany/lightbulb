import pytest
from unittest.mock import AsyncMock, patch
from core.services import jev
from core.services import voice_routing as vr

TEAM = [{"id": "ceo", "name": "CEO"}, {"id": "fin", "name": "Finance"}, {"id": "gro", "name": "Growth"}]
ROOM_CALL = {"kind": "room", "team": TEAM, "voiceAgentId": "ceo", "roomName": "general"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Finance, what's our burn?", "fin"),
        ("finance what's our burn", "fin"),
        ("ask Growth to draft a post", "gro"),
        ("can you tell finance to check payouts", "fin"),
        ("hey CEO what's next", "ceo"),
    ],
)
def test_named_agent(text, expected):
    assert vr.named_agent(text, TEAM) == expected


def test_name_mid_sentence_is_not_a_route():
    assert vr.named_agent("our growth is slow this month", TEAM) is None


@pytest.mark.asyncio
async def test_dm_calls_skip_routing():
    assert (
        await vr.pick(
            "org", {"kind": "dm", "voiceAgentId": "eng", "team": [{"id": "eng", "name": "Engineering"}]}, "x", "", None
        )
        == "eng"
    )


@pytest.mark.asyncio
async def test_name_beats_jev():
    with patch.object(vr.jev, "pick_agent", AsyncMock(return_value="gro")) as j:
        assert await vr.pick("org", ROOM_CALL, "Finance, burn?", "", "ceo") == "fin"
    j.assert_not_awaited()


@pytest.mark.asyncio
async def test_jev_pick_then_ceo_fallback():
    with patch.object(vr.jev, "pick_agent", AsyncMock(return_value="gro")):
        assert await vr.pick("org", ROOM_CALL, "draft a launch post", "", "ceo") == "gro"
    with patch.object(vr.jev, "pick_agent", AsyncMock(return_value=None)):
        assert await vr.pick("org", ROOM_CALL, "hmm", "", "ceo") == "ceo"


@pytest.mark.asyncio
async def test_fallback_is_voice_agent_when_ceo_not_on_team():
    call = {**ROOM_CALL, "team": TEAM[1:], "voiceAgentId": "fin"}
    with patch.object(vr.jev, "pick_agent", AsyncMock(return_value="ceo")):  # off-team pick ignored
        assert await vr.pick("org", call, "hmm", "", "ceo") == "fin"


async def _pick_agent(answer):
    with (
        patch.object(jev, "agent_card", AsyncMock(return_value="card")),
        patch.object(jev, "decide", AsyncMock(return_value=answer)) as d,
    ):
        out = await jev.pick_agent("org", room_name="general", team=TEAM, text="burn?", recent="earlier")
    return out, d.await_args.args


@pytest.mark.asyncio
async def test_pick_agent_confident():
    out, (state, qs) = await _pick_agent({"who": {"choice": "fin", "probabilities": {"fin": 0.9}}})
    assert out == "fin"
    assert state["room"] == {"kind": "channel", "name": "general"}
    assert state["latest"]["text"] == "burn?" and state["recent"][0]["text"] == "earlier"
    assert set(qs["who"]["criteria"]) == {"ceo", "fin", "gro"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"who": {"choice": "fin", "probabilities": {"fin": 0.4}}},
        None,
        "rate_limited",
    ],
)
async def test_pick_agent_unsure_or_down(answer):
    assert (await _pick_agent(answer))[0] is None


def test_longest_name_wins():
    team = [{"id": "c", "name": "Customer"}, {"id": "co", "name": "Customer Operations"}]
    assert vr.named_agent("Customer Operations, check refunds", team) == "co"
    assert vr.named_agent("Customer, check refunds", team) == "c"


def test_whitespace_and_metacharacters():
    team = [
        {"id": "co", "name": "Customer  Operations"},
        {"id": "rd", "name": "R&D (beta)"},
        {"id": "cpp", "name": "C++"},
    ]
    assert vr.named_agent("ask customer   operations to help", team) == "co"
    assert vr.named_agent("R&D (beta), status?", team) == "rd"
    assert vr.named_agent("hey C++ what now", team) == "cpp"
    assert vr.named_agent("heyfinance what", [{"id": "f", "name": "Finance"}]) is None


@pytest.mark.asyncio
async def test_pick_agent_decide_raises_or_off_criteria():
    with (
        patch.object(jev, "agent_card", AsyncMock(return_value="card")),
        patch.object(jev, "decide", AsyncMock(side_effect=RuntimeError("x"))),
    ):
        assert await jev.pick_agent("org", room_name="g", team=TEAM, text="t", recent="") is None
    assert (await _pick_agent({"who": {"choice": "zzz", "probabilities": {"zzz": 0.99}}}))[0] is None
