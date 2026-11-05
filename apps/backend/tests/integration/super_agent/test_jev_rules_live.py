"""One real Jev call with and without the rule question (wave2-S Task 8). Skipped unless AI_GATEWAY_API_KEY
holds the dev gateway key; two calls."""

import os
from unittest.mock import AsyncMock

import pytest

from core.services import jev

KEY = os.environ.get("AI_GATEWAY_API_KEY")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not KEY, reason="needs the dev gateway key")]

STATE = {
    "room": {"kind": "channel", "name": "dev"},
    "recent": [],
    "latest": {
        "author": "Priya",
        "kind": "human",
        "text": "Login page throws a 500 since this morning",
        "inThread": False,
    },
}
WHO = {
    "type": "choice",
    "instructions": jev.WHO_INSTRUCTIONS,
    "criteria": {"a1": "Lightbulb.", "none": jev.NONE_CRITERION},
}
RULE = {
    "type": "choice",
    "instructions": "The team wrote these rules for work an assistant may do in this room without being asked. Does "
    "the latest message clearly meet the 'when' condition of exactly one rule?",
    "criteria": {
        "bug": "When someone reports a bug, reproduce and fix it.",
        "none": "None: no rule's condition is met",
    },
}


async def test_jev_rules_live(monkeypatch):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=KEY))
    plain = await jev.decide(STATE, {"who": WHO})
    with_rule = await jev.decide(STATE, {"who": WHO, "rule": RULE})
    assert isinstance(plain, dict) and "rule" not in plain
    assert with_rule["rule"]["choice"] in {"bug", "none"}
    assert set(with_rule["rule"]["probabilities"]) <= {"bug", "none"}
