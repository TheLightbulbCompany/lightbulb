"""The one-voice meeting mode's gate and mixer (meet.py), and the 17 scenarios against the gate."""

import numpy as np
from livekit import rtc

from huddle_agent import meet
from replay import SCENARIOS, gate as replay_gate

PEOPLE = [{"name": "Maya"}, {"name": "Leo"}]


def g() -> meet.Gate:
    return meet.Gate(name="Nova", people=PEOPLE)


def test_speaks_only_when_named_after_a_pause():
    gate = g()
    assert (
        gate.heard("maya", "Leo, did you push the fix?", 0, new_turn=True) == "question"
    )
    assert (
        gate.heard("leo", "Yes, last night.", 2, new_turn=True) == ""
    )  # a person took the question
    assert gate.verdict(10, 8) is False  # nothing asked: a reply is dropped
    assert (
        gate.heard("maya", "Nova, how many calls did we miss?", 20, new_turn=True)
        == "ask"
    )
    assert gate.verdict(21, 1.0) is None  # the room is not quiet yet
    assert gate.verdict(23.5, meet.MEET_QUIET_S) is True


def test_an_unsaid_answer_goes_stale():
    gate = g()
    gate.heard("maya", "Nova, what's on tomorrow?", 0, new_turn=True)
    assert (
        gate.verdict(meet.STALE_S + 0.1, 0) is False
    )  # never quiet in time: dropped, not said late
    assert gate.ask_at is None


def test_unclaimed_question_after_three_and_a_half_seconds():
    gate = g()
    gate.heard("maya", "Does anyone know when the deploy finished?", 0, new_turn=True)
    assert gate.verdict(3.0, 3.0) is None
    assert gate.verdict(3.6, 3.6) is True


def test_stop_and_mentions():
    gate = g()
    gate.heard("maya", "Nova, read me the notes", 0, new_turn=True)
    assert (
        gate.heard("maya", "Nova, stop", 5, new_turn=True) == "stop"
        and gate.ask_at is None
    )
    assert (
        gate.heard("leo", "I asked Nova what the plan costs.", 9, new_turn=True) == ""
    )
    assert gate.heard("leo", "Hi Nova.", 12, new_turn=True) == "ask"


def test_follow_up_is_the_askers_question_with_nobody_in_between():
    gate = g()
    gate.heard("maya", "Nova, how many agents come with a workspace?", 0, new_turn=True)
    gate.answered(8)
    assert (
        gate.heard("maya", "And what are they called?", 30, new_turn=True) == "followup"
    )
    gate.answered(35)
    gate.heard("leo", "Cool.", 36, new_turn=True)
    assert (
        gate.heard("maya", "And when?", 40, new_turn=True) != "followup"
    )  # Leo spoke in between


def _frame(level: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        np.full(meet.FRAME, level, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME
    )


def test_mixer_sums_tracks_and_knows_who_spoke():
    m = meet.Mixer()
    m.push("meet:r:1", _frame(2000))
    m.push("meet:r:2", _frame(100))
    assert int(m.mix()[0]) == 2100
    assert m.speaker() == "meet:r:1"
    m.push("meet:r:1", _frame(32000))
    m.push("meet:r:2", _frame(32000))
    assert int(m.mix()[0]) == 32767  # clipped, not wrapped
    assert m.speaker(within_s=0) == ""  # nobody voiced in the window, two people on tracks
    m.remove("meet:r:2")
    assert m.speaker(within_s=0) == "meet:r:1"  # one person on a track: theirs


def test_the_name_may_open_any_sentence_of_a_joined_turn():
    gate = g()
    text = "Okay, everyone. Let's get started with the check-in. light bulb How many calls did we get?"
    assert meet.Gate(name="Lightbulb", people=PEOPLE).heard("p1", text, 0, new_turn=True) == "ask"
    assert gate.heard("p1", "Leo, did you push it? Nova, what's on tomorrow?", 0, new_turn=True) == "ask"
    assert gate.heard("p2", "We should ask Nova about it later.", 9, new_turn=True) == ""


def test_replay_scenarios_against_the_gate():
    results = [r for name in SCENARIOS for r in replay_gate.judge(name)]
    assert len({n for n in SCENARIOS}) == 17
    assert [r for r in results if r[0] == "FAIL"] == []
