"""Jev's Router question (§10, §14.7): one call per unaddressed message, Router + active specialists."""

import pytest

from core.services import jev

META = {"roomId": "r1", "ownerId": "org_1", "kind": "channel", "name": "general"}
MSG = {
    "messageId": "m1",
    "author": {"kind": "human", "id": "sam", "displayName": "Sam"},
    "text": "anyone reconcile Oct?",
    "createdAt": "2026-11-04T10:00:00Z",
}


@pytest.fixture
def jev_calls(monkeypatch):
    calls = []

    async def card(owner, agent_id, name, recency=""):
        return f"card:{name}"

    monkeypatch.setattr(jev, "agent_card", card)
    return calls


async def test_jev_route_for_router_question_shape(jev_calls, monkeypatch):
    async def decide(state, questions):
        jev_calls.append((state, questions))
        return {"who": {"choice": "fin", "probabilities": {"fin": 0.8, "lightbulb": 0.1, "none": 0.1}}}

    monkeypatch.setattr(jev, "decide", decide)
    out = await jev.route_for_router(META, MSG, [], [], {"fin": "Finance"})
    assert out == {"to": "router", "specialist": "fin", "confidence": 0.8}
    assert len(jev_calls) == 1
    state, questions = jev_calls[0]
    assert list(questions) == ["who"]
    assert set(questions["who"]["criteria"]) == {"lightbulb", "fin", "none"}
    assert questions["who"]["criteria"]["fin"] == "card:Finance"


@pytest.mark.parametrize(
    "answer,expected",
    [
        (None, {"fallback": True}),
        (jev.RATE_LIMITED, {"fallback": True}),
        ({"who": {"choice": "nobody"}}, {"fallback": True}),
        (
            {"who": {"choice": "none", "probabilities": {"none": 0.7}}},
            {"to": "none", "specialist": None, "confidence": 0.7},
        ),
        (
            {"who": {"choice": "lightbulb", "probabilities": {"lightbulb": 0.6}}},
            {"to": "router", "specialist": None, "confidence": 0.6},
        ),
    ],
)
async def test_jev_answers_map(jev_calls, monkeypatch, answer, expected):
    async def decide(state, questions):
        return answer

    monkeypatch.setattr(jev, "decide", decide)
    assert await jev.route_for_router(META, MSG, [], [], {"fin": "Finance"}) == expected


@pytest.mark.parametrize("first,calls", [(None, 2), (jev.RATE_LIMITED, 1)])
async def test_a_failed_jev_call_is_tried_once_more(jev_calls, monkeypatch, first, calls):
    """Dev walk 16: one Jev timeout (3 s cap, p99 ~0.7 s) dropped "FYI: we have floor cushions for 15 kids…" in
    silence. A timeout or error gets one more call; a rate limit does not."""
    answers = [first, {"who": {"choice": "lightbulb", "probabilities": {"lightbulb": 0.9}}}]

    async def decide(state, questions):
        jev_calls.append(1)
        return answers[len(jev_calls) - 1]

    monkeypatch.setattr(jev, "decide", decide)
    out = await jev.route_for_router(META, MSG, [], [], {})
    assert len(jev_calls) == calls
    assert out == ({"to": "router", "specialist": None, "confidence": 0.9} if calls == 2 else {"fallback": True})
