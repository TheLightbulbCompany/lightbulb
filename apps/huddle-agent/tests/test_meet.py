"""The one-voice meeting mode's gate and mixer (meet.py), and the 17 scenarios against the gate."""

import os
import time

import numpy as np
from livekit import rtc

from huddle_agent import meet
from huddle_agent.backend import Backend
from replay import SCENARIOS, gate as replay_gate

PEOPLE = [{"name": "Maya"}, {"name": "Leo"}]


def g() -> meet.Gate:
    return meet.Gate(name="Nova", people=PEOPLE)


def test_speaks_only_when_named_after_a_pause():
    gate = g()
    assert gate.heard("maya", "Leo, did you push the fix?", 0, new_turn=True) == ""
    assert (
        gate.heard("leo", "Yes, last night.", 2, new_turn=True) == ""
    )
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


def test_a_question_to_the_room_is_not_an_ask():
    gate = g()  # speaks only when named: the 3.5 s unclaimed-question rule is gone (transcripts come late)
    assert gate.heard("maya", "Does anyone know when the deploy finished?", 0, new_turn=True) == ""
    assert gate.verdict(5.0, 5.0) is False


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


def test_an_unnamed_follow_up_is_not_an_ask():
    gate = g()  # speaks only when named: "Leo, did you push the fix?" played as a follow-up live (7 Nov)
    gate.heard("maya", "Nova, how many agents come with a workspace?", 0, new_turn=True)
    gate.answered(8)
    assert gate.heard("maya", "And what are they called?", 30, new_turn=True) == ""
    assert gate.heard("maya", "Leo, did you push the fix last night?", 40, new_turn=True) == ""


def _frame(level: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        np.full(meet.FRAME, level, dtype=np.int16).tobytes(), meet.RATE, 1, meet.FRAME
    )


def test_mixer_sums_tracks_and_knows_when_anyone_spoke():
    m = meet.Mixer()
    before = m.voiced_at
    m.push("meet:r:1", _frame(100))  # below VOICED_RMS: on a track, not speaking
    m.mix()
    assert m.voiced_at == before
    m.push("meet:r:1", _frame(2000))
    m.push("meet:r:2", _frame(100))
    assert int(m.mix()[0]) == 2100 and m.voiced_at > before
    m.push("meet:r:1", _frame(32000))
    m.push("meet:r:2", _frame(32000))
    assert int(m.mix()[0]) == 32767  # clipped, not wrapped


def test_each_line_is_its_speakers_even_when_people_overlap():
    a = _agent()  # self-test 7 Nov: side talk before an ask made the member a guest; overlap lost the ask
    now = time.monotonic()
    a._mixer.voiced_at = now - 10
    a.heard("meet:r:p2#0", "Maya, did you send the invoice?", True, now, who="meet:r:p2")
    a.heard("meet:r:p2#1", "I think we should move the standup", False, now, who="meet:r:p2")
    a.heard("meet:r:p1#0", "Nova, how many calls this week?", False, now, who="meet:r:p1")
    a.heard("meet:r:p2#1", "I think we should move the standup to eight.", True, now, who="meet:r:p2")
    a.heard("meet:r:p1#0", "Nova, how many calls this week?", True, now, who="meet:r:p1")
    assert [t["seq"] for t in a.turns] == [1, 2, 3]  # one turn per line, not per event
    assert a.asker() == "p1" and a._authorize(now) == (True, "ask")


def _agent() -> meet.MeetAgent:
    os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
    os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "s")
    info = {"name": "Nova", "company": "C", "title": "T", "today": "d"}
    return meet.MeetAgent(Backend("r", "o", "t"), g(), meet.Mixer(), info, {"meet:r:p1": "Maya"})


def test_the_asker_is_who_said_the_latest_line():
    a = _agent()
    a.heard("i1", "Nova, how many calls?", False, time.monotonic(), who="meet:r:p1")
    assert a.asker() == "p1"  # the member: their own speech
    a.heard("i2", "Nova, read me the bookings", False, time.monotonic(), who="meet:r:p2")
    assert a.asker() == "p2"  # a guest: the backend answers needs_member
    assert meet.MeetAgent.asker(_agent()) == ""  # nobody spoke yet


def test_a_reply_is_held_until_its_turn_says_it_was_addressed():
    a = _agent()
    now = time.monotonic()
    a._mixer.voiced_at = now - 10  # the room is quiet
    assert a._authorize(now) == (None, "no transcript yet")  # audio ready, transcript not: hold
    a.heard("i1", "Okay, let's start.", False, now)
    assert a._authorize(now) == (None, "turn still coming")
    a.heard("i1", "Okay, let's start. Leo, did you push it?", True, now)
    assert a._authorize(now) == (False, "not addressed")  # dropped unheard
    a.consumed = 1
    a.heard("i2", "Nova, how many calls this week?", True, now)
    assert a._authorize(now) == (True, "ask")
    a._mixer.voiced_at = now - 1
    assert a._authorize(now) == (None, "room not quiet")


def test_a_turn_authorizes_one_reply_and_a_follow_on_only_after_a_tool_with_nobody_speaking():
    a = _agent()
    now = time.monotonic()
    a._mixer.voiced_at = now - 10
    t = a.heard("i1", "Nova, how many calls?", True, now)
    assert a._authorize(now) == (True, "ask")
    a.reply_turn, a.consumed = t, t["seq"]  # what the node does when it plays
    assert a._authorize(now) == (None, "no transcript yet")  # a second reply needs its own turn...
    a.tool_at = now  # a tool ran after the turn's final transcript (Gemini: transcript -> tool -> answer)
    assert a._authorize(now) == (True, "tool follow-on")  # ...unless Gemini answers after a tool it called
    a._mixer.voiced_at = now + 1  # someone spoke after the turn: the reply may be for that talk
    assert a._authorize(now + 2) == (None, "no transcript yet")


def test_a_partial_never_releases_a_reply():
    a = _agent()  # a partial is a prefix as the transcriber streams it ("Light", then "Lightbulb, can you")
    now = time.monotonic()
    a._mixer.voiced_at = now - 10
    a.heard("i1", "Okay, let's start. Nova, how many", False, now)
    assert a._authorize(now) == (None, "turn still coming")
    a.heard("i1", "Can", False, now)
    assert a._authorize(now) == (None, "turn still coming")  # no name yet is not "not addressed"
    a.heard("i1", "Okay, let's start. Nova, how many calls did we get?", True, now)
    assert a._authorize(now) == (True, "ask")


def test_a_name_in_a_short_partial_does_not_wake_it_the_final_decides():
    a = _agent()
    now = time.monotonic()
    a._mixer.voiced_at = now - 10
    t = a.heard("i1", "We should ask Nova", False, now)  # 4 words: the short-line rule would wake on it
    assert t["kind"] == ""
    a.heard("i1", "We should ask Nova about it later.", True, now)
    assert t["kind"] == "" and a._authorize(now) == (False, "not addressed")
    t2 = a.heard("i2", "Nova, stop", False, now)  # a partial may still stop it
    assert t2["kind"] == "stop"
    a.heard("i2", "Nova, stop talking please, thanks", True, now)
    assert t2["kind"] == "stop"


def test_the_name_may_open_any_sentence_of_a_joined_turn():
    gate = g()
    text = "Okay, everyone. Let's get started with the check-in. light bulb How many calls did we get?"
    assert meet.Gate(name="Lightbulb", people=PEOPLE).heard("p1", text, 0, new_turn=True) == "ask"
    assert gate.heard("p1", "Leo, did you push it? Nova, what's on tomorrow?", 0, new_turn=True) == "ask"
    assert gate.heard("p2", "We should ask Nova about it later.", 9, new_turn=True) == ""


def test_replay_scenarios_against_the_gate():
    """The live order: the reply's audio is held until its turn's transcript arrives (TRANSCRIPT_LAG_S). The
    assistant must stay silent where it should, at any lag; how soon it answers (ack_within) depends on the lag
    and is a measurement, reported by `python -m tests.replay.gate`, not asserted here."""
    assert len(set(SCENARIOS)) == 17
    for lag in (0.0, 2.0, replay_gate.TRANSCRIPT_LAG_S, 8.0):
        replay_gate.TRANSCRIPT_LAG_S = lag
        results = [r for name in SCENARIOS for r in replay_gate.judge(name)]
        assert [r for r in results if r[0] == "FAIL" and r[1] == "silent_between"] == [], lag


async def test_reads_and_chat_name_the_asker():
    """Gate B1: memory, calendar and chat are the member's own speech; the backend needs the asker to tell."""
    a, sent = _agent(), []

    async def post(op, body):
        sent.append((op, body))
        return {}

    a._post = post
    a.asker = lambda: "p1"
    await a.recall_memory("prices?")
    await a.calendar("2026-11-09")
    await a.send_to_chat("Ana at 10")
    assert [b.get("participantId") for _, b in sent] == ["p1", "p1", "p1"]
