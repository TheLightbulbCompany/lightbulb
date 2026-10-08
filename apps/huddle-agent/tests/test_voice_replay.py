"""The live rig's 15 scenarios, replayed offline against the real controller (tests/replay), and the worker's
recorder. `pytest -m replay` is the gate: a check that fails and is not a known failure is a regression."""

import asyncio
import json

import pytest
from huddle_agent import record
from replay import ROUTING, SCENARIOS, Sim, judge, known, live, load, replay, routing
from replay.sim import EPOCH, LEAD, PROFILES

SEEDS = 10  # the full loop is `python -m tests.replay all --seeds 100`, under both profiles; this is the quick gate


@pytest.mark.replay
@pytest.mark.parametrize("profile", PROFILES)
def test_the_replay_shows_no_new_failure_and_no_known_one_grown(profile):
    """Every scenario, SEEDS seeds: no check fails more often than its baseline rate allows (`routing.gate`).
    That holds a known failure to the rate it is known at, and a check that passed to passing."""
    fails = {}
    for name in SCENARIOS:
        for seed in range(SEEDS):
            for status, what, _ in replay(name, seed, profile=profile)[0]:
                if status == "FAIL":
                    key = f"{name} | {what.split('  #')[0]}"
                    fails[key] = fails.get(key, 0) + 1
    assert not (worse := routing.gate(routing.baseline()[profile], fails, SEEDS)), "\n".join(worse)


def test_every_failure_in_the_baseline_has_its_cause_written_down():
    for profile in PROFILES:
        for key in routing.baseline()[profile]["fails"]:
            name, _, what = key.partition(" | ")
            assert known(name, what), f"{profile}: {key} fails in the baseline and routing.KNOWN_FAILING does not say why"


def test_the_gate_fails_a_known_failure_that_got_worse_and_a_check_that_used_to_pass():
    base = {"seeds": 200, "fails": {"gap | ack_within": 40, "gap | no_talk_over": 2}}
    assert routing.gate(base, {"gap | ack_within": 4, "gap | no_talk_over": 1}, 20) == [], "at its rate, and inside the noise"
    assert routing.allowed(base, "gap | ack_within", 20) == 12 and routing.allowed(base, "gap | said_once", 20) == 0
    worse = routing.gate(base, {"gap | ack_within": 13}, 20)[0]
    assert "WORSE" in worse and "gap | ack_within" in worse, "known to fail one seed in five, not two in three"
    (new,) = routing.gate(base, {"gap | said_once": 1}, 20)
    assert "NEW" in new
    many = {"seeds": 200, "fails": {f"s{i} | ack_within": 40 for i in range(30)}}
    spread = {key: 7 for key in many["fails"]}  # each inside its own tolerance, and three in four of them up
    assert all(7 <= routing.allowed(many, key, 20) for key in spread)
    assert [line.split(":")[0] for line in routing.gate(many, spread, 20)] == ["all checks together"]


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
