"""The rule question rides Jev's one per-message call (wave2-S Task 3, spec 12 §3.3)."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import jev, rules

pytestmark = pytest.mark.asyncio

META = {"ownerId": "org_a", "roomId": "r1", "kind": "channel", "name": "dev"}
MEMBERS = [
    {"id": "u1", "kind": "human", "displayName": "Sam"},
    {"id": "a1", "kind": "agent", "displayName": "Lightbulb"},
]
MESSAGE = {
    "messageId": "m1",
    "author": {"kind": "human", "id": "u1", "displayName": "Sam"},
    "text": "login page 500s since this morning",
    "createdAt": "2026-11-04T10:00:00Z",
    "dispatchTargets": [{"eventKind": "room_event", "agentId": "a1"}],
}
RULES = [
    {"scheduleId": "s1", "policyText": "When someone reports a bug, reproduce it", "createdAt": "1"},
    {"scheduleId": "s2", "policyText": "When someone asks for the deck, find it", "createdAt": "2"},
]
WHO = {"choice": "none", "probabilities": {"a1": 0.2, "none": 0.8}}
PLACEMENT = {"choice": "main"}


async def _run(monkeypatch, answers, **kw):
    decide = AsyncMock(return_value=answers)
    monkeypatch.setattr(jev, "decide", decide)
    monkeypatch.setattr(jev, "agent_card", AsyncMock(return_value="card"))
    monkeypatch.setattr(jev, "put_metric", lambda *a, **k: None)
    out = await jev._decide_for_message(META, MESSAGE, MESSAGE, MEMBERS, [], **kw)
    return out, decide.await_args.args


async def test_rules_empty_request_byte_identical(monkeypatch):
    answers = {"who": WHO, "placement": PLACEMENT}
    plain, plain_args = await _run(monkeypatch, answers)
    empty, empty_args = await _run(monkeypatch, answers, rules=())
    assert json.dumps(empty_args, sort_keys=True) == json.dumps(plain_args, sort_keys=True)
    assert empty == plain and "rule" not in plain


async def test_rule_question_added(monkeypatch):
    _, (_, questions) = await _run(monkeypatch, {"who": WHO, "placement": PLACEMENT}, rules=RULES)
    assert questions["rule"] == rules.jev_question(RULES)
    assert set(questions["rule"]["criteria"]) == {"s1", "s2", "none"}


async def test_rule_match_returned(monkeypatch):
    rule = {"choice": "s1", "probabilities": {"s1": 0.9, "s2": 0.02, "none": 0.08}}
    without, _ = await _run(monkeypatch, {"who": WHO, "placement": PLACEMENT})
    out, _ = await _run(monkeypatch, {"who": WHO, "placement": PLACEMENT, "rule": rule}, rules=RULES)
    assert out["rule"] == "s1" and out["ruleP"] == 0.9
    assert {k: out[k] for k in without} == without


async def test_jev_misfire_unknown_rule_id(monkeypatch):
    rule = {"choice": "s9", "probabilities": {"s9": 0.99}}
    without, _ = await _run(monkeypatch, {"who": WHO, "placement": PLACEMENT})
    out, _ = await _run(monkeypatch, {"who": WHO, "placement": PLACEMENT, "rule": rule}, rules=RULES)
    assert out["rule"] is None and out["ruleP"] is None
    assert out["who"] == without["who"]


async def test_jev_misfire_bad_who_no_rule(monkeypatch):
    """A bad `who` is a who-fallback; the rule answer is parsed on its own, so it neither drops nor invents one."""
    bad_who = {"choice": "ghost", "probabilities": {"ghost": 1.0}}
    rule = {"choice": "s1", "probabilities": {"s1": 0.95, "none": 0.05}}
    out, _ = await _run(monkeypatch, {"who": bad_who, "placement": PLACEMENT, "rule": rule}, rules=RULES)
    assert out == {"fallback": True, "reason": "invalid_choice", "rule": "s1", "ruleP": 0.95}
    out, _ = await _run(monkeypatch, {"who": bad_who, "placement": PLACEMENT}, rules=RULES)
    assert out == {"fallback": True, "reason": "invalid_choice", "rule": None, "ruleP": None}


@pytest.mark.parametrize("answers", [None, jev.RATE_LIMITED])
async def test_jev_down_no_match(monkeypatch, answers):
    out, _ = await _run(monkeypatch, answers, rules=RULES)
    assert out["rule"] is None
    assert out.get("fallback") or out.get("rateLimited")


async def test_jev_timeout_no_match(monkeypatch):
    """A real `decide` whose gateway times out: no match, never a guess."""

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(slow), **kw))
    monkeypatch.setattr(jev, "agent_card", AsyncMock(return_value="card"))
    monkeypatch.setattr(jev, "put_metric", lambda *a, **k: None)
    out = await jev._decide_for_message(META, MESSAGE, MESSAGE, MEMBERS, [], rules=RULES)
    assert out == {"fallback": True, "rule": None}
