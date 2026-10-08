"""The live rig's 15 scenarios and one of the router's own (mentioned_not_asked), replayed offline against the
real controller (tests/replay), and the worker's recorder. `pytest -m replay` is the gate: a check that fails and is
not a known failure is a regression."""

import asyncio
import json
import os

import pytest
from huddle_agent import huddle as h_mod
from huddle_agent import record
from replay import ROUTING, SCENARIOS, Sim, judge, known, live, load, replay, router, routing
from replay.sim import EPOCH, LEAD, P_RUN_NEVER, P_RUN_SLOW, PROFILES, Model

SEEDS = range(10)  # the full loop is `python -m tests.replay all --seeds 100`, under both profiles; this is the quick gate


def failing(profile, seeds=SEEDS, names=SCENARIOS):
    fails = {}
    for name in names:
        for seed in seeds:
            for status, what, _ in replay(name, seed, profile=profile)[0]:
                if status == "FAIL":
                    key = f"{name} | {what.split('  #')[0]}"
                    fails[key] = fails.get(key, 0) + 1
    return fails


@pytest.mark.replay
@pytest.mark.parametrize("profile", PROFILES)
def test_the_replay_shows_no_new_failure_and_no_known_one_grown(profile):
    """Every scenario over SEEDS: no check fails more often than it did on the same seeds when the baseline
    was taken, beyond a small tolerance (`routing.gate`). That holds a known failure to the rate it is known
    at, and a check that passed to passing."""
    assert not (worse := routing.gate(routing.baseline()[profile], failing(profile), SEEDS)), "\n".join(worse)


@pytest.mark.replay
def test_the_gate_sees_a_regression_inside_a_known_failure(monkeypatch):
    """backchannel's keeps_speaking is a known failure (18% of seeds). A controller that lets a listening
    sound stop a sentence fails it far more often, and no check that passed starts to fail: the old gate,
    which only knew that the check may fail, passed that controller."""
    monkeypatch.setattr(h_mod.Huddle, "_only_listening", lambda self, identity: False)
    base = routing.baseline()["current"]
    part = {**base, "fails": {k: v for k, v in base["fails"].items() if k.startswith("backchannel |")}}
    worse = routing.gate(part, failing("current", names=["backchannel"]), SEEDS)
    assert any("keeps_speaking" in line and "WORSE" in line for line in worse), worse


def test_every_failure_in_the_baseline_has_its_cause_written_down():
    for profile in PROFILES:
        for key in routing.baseline()[profile]["fails"]:
            name, _, what = key.partition(" | ")
            assert known(name, what), f"{profile}: {key} fails in the baseline and routing.KNOWN_FAILING does not say why"


def test_the_gate_fails_a_known_failure_that_got_worse_and_a_check_that_used_to_pass():
    base = {"seeds": 200, "fails": {"gap | ack_within": list(range(0, 200, 5)), "gap | no_talk_over": [7, 150]}}
    first = range(20)  # of these the baseline failed ack_within on 0, 5, 10, 15 and no_talk_over on 7
    assert routing.gate(base, {"gap | ack_within": 4, "gap | no_talk_over": 1}, first) == [], "as it was"
    assert routing.gate(base, {"gap | ack_within": 5, "gap | no_talk_over": 2}, first) == [], "a replay or two flipped"
    assert routing.allowed(base, "gap | ack_within", first) == 7 and routing.allowed(base, "gap | said_once", first) == 0
    worse = routing.gate(base, {"gap | ack_within": 8}, first)[0]
    assert "WORSE" in worse and "gap | ack_within" in worse, "known to fail one seed in five, not two in five"
    (new,) = routing.gate(base, {"gap | said_once": 1}, first)
    assert "NEW" in new
    many = {"seeds": 200, "fails": {f"s{i} | ack_within": list(range(0, 200, 5)) for i in range(30)}}
    spread = {key: 6 for key in many["fails"]}  # each inside its own tolerance, and every one of them up
    assert [line.split(":")[0] for line in routing.gate(many, spread, first)] == ["all checks together"]
    # seeds the baseline does not hold: by its rates
    assert routing.gate(base, {"gap | ack_within": 8}, range(200, 220)) == []
    assert "WORSE" in routing.gate(base, {"gap | ack_within": 13}, range(200, 220))[0]


def test_the_replay_decides_with_the_backends_own_router():
    """/route in a replay is apps/backend's `route_live_line`, not a copy: its file, and its bars."""
    vc = router.backend()
    assert vc.__file__.endswith(os.path.join("apps", "backend", "core", "services", "voice_calls.py"))
    assert vc.route_live_line.__module__ == "core.services.voice_calls" and vc.messaging.address.__code__.co_filename.endswith("messaging.py")
    # one line through it: the classifier's labels are the table's, the request is the router's own cut of the line
    scenario = load("never_mind")
    sim = Sim(scenario, ROUTING["never_mind"], "never_mind:0")
    body = {"setting": "meeting", "open": [], "engagements": []}
    reply = asyncio.run(sim.router.route("Prasiddha", scenario["lines"][0]["say"], "", 2, body))
    assert reply["ops"]["requests"] == [{"agentId": "friday", "task": "what does the Workspace plan cost per month", "by": "name"}]
    assert reply["ops"]["say"]["kind"] == "ack"


def test_the_replay_gets_a_classifier_that_is_sometimes_wrong_and_runs_that_are_sometimes_late():
    """The mistakes the live run showed are in the model at their rates, seeded: a named ask the classifier
    misses (its words read as people talk, or its name as no name), a run that takes most of a minute and
    one that never ends. And the router's rule for the first holds on every one of them."""
    scenario, line = load("never_mind"), load("never_mind")["lines"][0]["say"]
    state = {"latest": {"text": line, "author": "Prasiddha"}, "room": {}}
    questions = {
        "k0": {"instructions": 'What is it? The part: "what does the Workspace plan cost per month".', "criteria": {"request": "", "people": ""}},
        "n0": {"instructions": "How is it used? The name: 'friday' (an AI teammate).", "criteria": {"addressed": "", "not_a_name": ""}},
    }
    body = {"setting": "meeting", "open": [], "engagements": []}
    wrong = 0
    for i in range(300):
        sim = Sim(scenario, ROUTING["never_mind"], f"s{i}")
        said = sim.router.jev.decide(state, questions)
        missed = said["k0"]["choice"] != "request" or said["n0"]["choice"] != "addressed" or said["k0"]["probabilities"]["request"] < 0.5
        wrong += missed
        if missed:  # (the rule: a request said to an agent by name is that agent's, whatever the classifier doubts)
            assert asyncio.run(sim.router.route("Prasiddha", line, "", 2, body))["ops"]["requests"], i
    assert 0.03 < wrong / 300 < 0.2, "a named ask is misread now and then, not never and not often"
    took = [Model(f"m{i}").run() for i in range(2000)]
    never, slow = sum(t is None for t in took) / 2000, sum(t is not None and t >= 45 for t in took) / 2000
    assert abs(never - P_RUN_NEVER) < 0.02 and abs(slow - P_RUN_SLOW) < 0.03
    assert all(5 <= t <= 12 or 45 <= t <= 110 for t in took if t is not None)


@pytest.mark.replay
def test_a_replay_is_the_same_every_time():
    a, b = replay("sales_call", 3)[1], replay("sales_call", 3)[1]
    assert (a.rig, a.emf, [(x.at, x.name, x.text) for x in a.lines]) == (b.rig, b.emf, [(x.at, x.name, x.text) for x in b.lines])


@pytest.mark.replay
def test_a_recorded_call_replays_to_the_same_words(tmp_path):
    """The worker's recorder on one call, and the replayer on its file: the people, /route's answers and the
    runs come from the recording, so the same things are asked and the same sentences said."""
    name, tape = "two_agents", str(tmp_path / "call.jsonl")
    scenario = load(name)
    first = Sim(scenario, ROUTING[name], f"{name}:0", tape=tape).run()
    rec = live.load(tape, scenario)
    assert {e["k"] for e in rec.events} >= {"on_speech", "on_active_speakers", "on_final_transcript", "call", "on_data", "on_unit", "live"}
    # the recording's clock is the first call's loop clock: scenario second 0 is LEAD on it
    rec.zero, said = LEAD, {}
    for t, kind, detail in first.rig:
        if kind == "SAY":
            said[detail.partition(": ")[0]] = (*detail.partition(": ")[::2], t - EPOCH)
        elif kind == "SAID":
            rec.says.append((*said.pop(detail), t - EPOCH))
    again = Sim(scenario, {}, f"{name}:1", recording=rec).run()  # no routing table: only what was recorded
    assert not again.notes and not again.errors
    words = lambda ev: [(x.name, x.text) for x in ev.lines]  # noqa: E731
    assert words(again) == words(first)
    assert [s for s, _, _ in judge(scenario, again)] == [s for s, _, _ in judge(scenario, first)]


def test_a_recording_holds_inputs_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv(record.ENV, str(tmp_path))
    rec = record.start("room-1")

    class Backend:
        async def route(self, speaker, utterance, transcript, humans=None):
            return {"agentId": "forge", "roomToken": "s3cret", "link": "see https://example.com/x?token=abc"}

    class Huddle:
        def on_speech(self, identity, started):
            self.on_active_speakers({identity})  # the controller calling itself is not an input

        def on_active_speakers(self, identities):
            pass

    h = Huddle()
    monkeypatch.setattr(record, "INPUTS", ("on_speech", "on_active_speakers"))
    rec.inputs(h)
    h.on_speech("u1", True)
    asyncio.run(rec.backend(Backend()).route("u1", "Forge, hello", "a long transcript", humans=2))
    lines = [json.loads(x) for x in next(tmp_path.iterdir()).read_text().splitlines()]
    assert [x["k"] for x in lines] == ["start", "on_speech", "call"]
    assert lines[1] == {"t": lines[1]["t"], "k": "on_speech", "identity": "u1", "started": True}
    call = lines[2]
    assert call["m"] == "route" and call["args"] == {"speaker": "u1", "utterance": "Forge, hello", "humans": 2}
    assert call["reply"] == {"agentId": "forge", "link": "see [url]"} and "s3cret" not in json.dumps(lines)


def test_no_recorder_without_the_switch(monkeypatch):
    monkeypatch.delenv(record.ENV, raising=False)
    backend = object()
    assert record.start("room-1").backend(backend) is backend
