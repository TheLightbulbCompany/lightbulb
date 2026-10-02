import pytest
from unittest.mock import AsyncMock, patch
from core.services import jev
from core.services import voice_routing as vr

TEAM = [{"id": "ceo", "name": "CEO"}, {"id": "fin", "name": "Finance"}, {"id": "gro", "name": "Growth"}]


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


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Lightbulb, what did Growth find?", "gro"),
        ("is finance or growth on this?", "fin"),  # first mentioned
        ("lightbulb, anything new", None),
        ("refinance the loan", None),  # whole words only
    ],
)
def test_mentioned_agent_anywhere(text, expected):
    assert vr.mentioned_agent(text, TEAM) == expected


def test_mentioned_agent_prefers_the_longest_name_at_a_position():
    team = [{"id": "c", "name": "Customer"}, {"id": "co", "name": "Customer Operations"}]
    assert vr.mentioned_agent("lightbulb, can customer operations check refunds", team) == "co"


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


# The same phrases as apps/huddle-agent's PARITY table: the worker's fast path names exactly who chat's
# opening-name rule names, so every other line reaches Jev.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hey guys.", set()),
        ("Hey guys, hey Friday.", set()),
        ("Hey guys. Hey Friday, what's up?", set()),
        ("Hey Friday, what's up?", {"fri"}),
        ("Friday, can you check revenue?", {"fri"}),
        ("Gauge, what's revenue?", {"gau"}),
    ],
)
def test_opening_name_parity(text, expected):
    from core.services import messaging

    members = [
        {"kind": "agent", "id": "fri", "displayName": "Friday"},
        {"kind": "agent", "id": "gau", "displayName": "Gauge"},
    ]
    assert messaging._addressed_by_name(text, members) == expected
