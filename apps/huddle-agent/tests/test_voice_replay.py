"""The live rig's 15 scenarios, replayed offline against the real controller (tests/replay), and the worker's
recorder. `pytest -m replay` is the gate: a check that fails and is not a known failure is a regression."""

import asyncio
import json

import pytest
from huddle_agent import record
from replay import ROUTING, SCENARIOS, Sim, judge, known, live, load, replay
from replay.sim import EPOCH, LEAD

SEEDS = 5  # the full loop is `python -m tests.replay all` (20 seeds); this is the quick gate


@pytest.mark.replay
@pytest.mark.parametrize("name", SCENARIOS)
def test_a_scenario_replays_with_no_new_failure(name):
    new = {}
    for seed in range(SEEDS):
        for status, what, why in replay(name, seed)[0]:
            if status == "FAIL" and known(name, what) is None:
                new.setdefault(what, f"seed {seed}: {why}")
    assert not new, "\n".join(f"{what}\n    {why}" for what, why in new.items())


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
