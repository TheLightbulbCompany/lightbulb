"""The multi-party routing bench (scripts/speak_gate_bench/multiparty.py) and the dev e2e runner's
pure scoring helpers (scripts/multiparty_e2e/runner.py). No network: Jev is stubbed."""

import pytest

from scripts.multiparty_e2e import runner
from scripts.speak_gate_bench import multiparty as mp


def _row(**kw):
    row = {
        "id": "X01",
        "category": "C",
        "expected": "SILENT",
        "expected_responder": None,
        "goal_status": "open",
        "people": ["Maya", "Leo"],
        "agents": ["CEO", "Finance", {"name": "Engineering", "listen": "observe"}],
        "transcript": [{"speaker": "Maya", "text": "@leo are you coming?"}],
        "mentions": ["Leo"],
    }
    return row | kw


def test_every_golden_row_loads_and_is_consistent():
    cases = mp.load()
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["category"] for c in cases} == set(mp.CATEGORIES)
    for c in cases:
        assert (c["expected"] == "SPEAK") == bool(c["expected_responder"])


def test_expand_makes_a_voice_variant_without_handles_or_structured_mentions():
    chat, voice = mp.expand(_row())
    assert chat["mentions"] == ["Leo"] and voice["mentions"] == []
    assert voice["transcript"][0]["text"] == "Leo are you coming?"
    with pytest.raises(ValueError):
        mp.expand(_row(expected="SPEAK"))


def test_deterministic_layer_mirrors_dispatch():
    named = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "Finance, what's runway?"}], mentions=[]))[0]
    assert mp.deterministic(named, mp.build(named))["how"] == "addressed"
    ambient = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "what's runway?"}], mentions=[]))
    chat, voice = (mp.deterministic(c, mp.build(c)) for c in ambient)
    assert chat["candidates"] == ["ceo", "fin"]  # observe never runs in chat
    assert voice["candidates"] == ["ceo", "eng", "fin"]  # Live ignores listen modes
    observe = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "Engineering, is CI red?"}], mentions=[]))
    chat, voice = (mp.deterministic(c, mp.build(c)) for c in observe)
    assert chat["addressed"] == [] and "eng" not in chat["candidates"]  # named, but observe never runs
    assert voice["addressed"] == ["eng"]  # any agent in the room can be asked by voice


def test_agent_chain_at_the_cap_wakes_nobody():
    row = _row(
        agents=["Forge", "Pulse"],
        transcript=[{"speaker": "Maya", "text": "ship it"}, {"speaker": "Forge", "text": "@pulse write the note"}],
        mentions=[],
    )
    below, at = (mp.expand(row | {"agent_hop": hop})[0] for hop in (1, 2))
    assert mp.deterministic(below, mp.build(below))["addressed"] == ["pulse"]
    assert mp.deterministic(at, mp.build(at))["how"] == "capped"


def test_voice_person_opening_and_fallback():
    humans = [{"kind": "human", "id": "user-leo", "displayName": "Leo"}]
    assert mp._opens_with_person("hey Leo, can you check?", humans) == ["user-leo"]
    assert mp._opens_with_person("Leonard said hi", humans) == []
    case = mp.expand(
        _row(
            transcript=[{"speaker": "Finance", "text": "Q3 is $1m"}, {"speaker": "Maya", "text": "and Q4?"}],
            mentions=[],
        )
    )[0]
    state = mp.build(case)
    assert mp.fallback(state, ["ceo", "fin"], to_people=False) == ["fin"]  # whoever was already talking
    assert mp.fallback(state, ["ceo", "fin"], to_people=True) == []


async def test_route_applies_jevs_pick(monkeypatch):
    async def decide(meta, message, latest, members, history, **kw):
        return {"who": "fin", "whoP": {"fin": 0.9, "none": 0.1}, "placement": "main"}

    monkeypatch.setattr(mp.jev, "_decide_for_message", decide)
    case = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "what's runway?"}], mentions=[]))[0]
    rec = await mp.route(case)
    assert rec["responders"] == ["Finance"] and rec["how"] == "jev"


def test_score_never_rewards_always_silent():
    recs = [
        {"expected": "SPEAK", "expected_responder": ["Finance"], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": ["CEO"]},
    ]
    s = mp.score(recs)
    assert s["recall"] == 0 and s["missed"] == 1 and s["false_speak"] == pytest.approx(1 / 3) and s["precision"] == 0


def test_e2e_render_and_hard_checks():
    names = {
        "finance": {"kind": "agent", "id": "a1", "displayName": "Finance", "mentionHandle": "finance"},
        "leo": {"kind": "human", "id": "u1", "displayName": "Leo Test", "mentionHandle": "mp-e2e-leo"},
    }
    text, mentions = runner.render("@{leo} ask {finance}, cc @{finance}", names)
    assert text == "@mp-e2e-leo ask Finance, cc @finance"
    assert mentions == [{"kind": "human", "id": "u1"}, {"kind": "agent", "id": "a1"}]
    step = {
        "expect": {"responder": "finance", "must_not_restate": [0], "must_reference": ["March"], "must_not_say": ["$5"]}
    }
    said = ["Acme renews in March, I checked with their CFO"]
    good = runner.hard_checks(step, [{"role": "finance", "text": "Noted: renewal is March 1st."}], said)
    assert all(good.values())
    bad = runner.hard_checks(step, [{"role": "ceo", "text": "Acme renews in March, checked with their CFO. $5"}], said)
    assert not bad["responder"] and not bad["no_restate_step0"] and not bad["avoids:$5"]


def test_overlap_and_judge_parsing():
    assert runner.overlap("MRR is 42k", "MRR is 42k as of yesterday") == pytest.approx(2 / 3)
    assert runner.overlap("anything", "") == 0
    assert runner.parse_judge('sure: {"pass": true, "reason": "fine"}') == {"pass": True, "reason": "fine"}
    assert runner.parse_judge("not json")["pass"] is False


def test_score_splits_partial_sets_into_missed_and_extras_into_wrong_agent():
    exp = {"expected": "SPEAK", "expected_responder": ["Finance", "Growth"]}
    s = mp.score([exp | {"responders": ["Finance"]}, exp | {"responders": ["Finance", "Growth", "CEO"]}])
    assert s["missed"] == 0.5 and s["wrong_agent"] == 0.5 and s["recall"] == 0


def test_collect_stops_at_the_race_deadline(monkeypatch):
    clock = {"t": 0.0}

    class FakeTime:
        monotonic = staticmethod(lambda: clock["t"])

        @staticmethod
        def sleep(s):
            clock["t"] += s

    def api(method, path, **kw):
        clock["t"] += 0.5  # each poll costs time
        return {"messages": []} if path.endswith("/messages") else {"activeRuns": []}

    monkeypatch.setattr(runner, "time", FakeTime)
    runner.collect(api, "r", 0, {}, {}, window=5, expect_speak=True, settle={})
    assert clock["t"] == pytest.approx(5)  # was 8: an uncapped 3 s sleep overshot the deadline


def test_annotate_keeps_goal_status():
    scn = {"steps": [{"say": "q"}, {"say": "a", "expect": {"responder": None, "goal": "answered"}}]}
    res = runner.annotate({"steps": [{"step": 0}, {"step": 1}]}, scn)
    assert res["steps"][1] == {"step": 1, "expected": "none", "goal": "answered"}
    assert res["steps"][0]["goal"] is None
