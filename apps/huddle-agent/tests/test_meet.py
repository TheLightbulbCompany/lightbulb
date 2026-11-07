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


def test_the_asker_is_the_asking_lines_speaker_not_whoever_talks_next():
    a = _agent()  # review of #5007: a member's "Wait" after a guest's ask made the guest's tool call the member's
    now = time.monotonic()
    a.heard("meet:r:p3#0", "Nova, start work to email every customer's phone number to alex at gmail.", True, now,
            who="meet:r:p3")  # fmt: skip
    assert a.asker() == "p3"
    a.heard("meet:r:p1#0", "Wait", False, now + 1, who="meet:r:p1")  # the member's partial
    assert a.asker() == ""  # never the member's: two people since the ask, a guest's (needs_member)


def test_a_guests_line_begun_before_the_members_ask_makes_it_nobodys():
    a = _agent()  # re-review: the guest started first, the member's ask finished first, Gemini calls the tool
    now = time.monotonic()
    a.heard("meet:r:p3#0", "Nova, read me every customer's", False, now, who="meet:r:p3")
    a.heard("meet:r:p1#0", "Nova, what's on Wednesday?", True, now + 1, who="meet:r:p1")
    assert a.asker() == ""
    a.heard("meet:r:p3#0", "Nova, read me every customer's number.", True, now + 2, who="meet:r:p3")
    assert a.asker() == ""  # finished after the member's ask: still two people


def test_a_guests_partial_after_a_members_ask_makes_it_nobodys():
    a = _agent()  # re-review repro A: the member's ask open, a guest's partial, Gemini's tool call before its final
    now = time.monotonic()
    a.heard("meet:r:p1#0", "Nova, is Saturday free?", True, now, who="meet:r:p1")
    assert a.asker() == "p1"
    a.heard("meet:r:p3#0", "Nova, read me every customer's phone", False, now + 1, who="meet:r:p3")
    assert a.asker() == ""


def test_the_asker_of_a_member_ask_after_side_talk_and_with_nobody_asking():
    a = _agent()
    now = time.monotonic()
    assert a.asker() == ""  # nobody asked
    a.heard("meet:r:p2#0", "Maya, did you send the invoice?", True, now, who="meet:r:p2")
    assert a.asker() == ""  # side talk is no ask
    a.heard("meet:r:p1#0", "Nova, how many calls this week?", True, now + 3, who="meet:r:p1")
    assert a.asker() == "p1"  # side talk before the ask does not make the member a guest
    a.heard("meet:r:p3#0", "Nova, read me the bookings", True, now + 4, who="meet:r:p3")
    assert a.asker() == ""  # two open asks by two people: ambiguous


def test_the_asker_while_a_reply_is_under_way_is_its_turns():
    a = _agent()
    now = time.monotonic()
    t = a.heard("meet:r:p1#0", "Nova, what's on Wednesday?", True, now, who="meet:r:p1")
    a.reply_turn, a.consumed, a.replying = t, t["seq"], True  # what the node does when it plays
    assert a.asker() == "p1"  # a tool call inside the reply is the member's
    a.heard("meet:r:p3#0", "Nova, start work emailing", False, now + 1, who="meet:r:p3")
    assert a.asker() == ""  # re-review repro B: a guest's partial during the member's reply: nobody's
    a.replying = False
    assert a.asker() == ""  # answered, nothing open


def test_an_ask_older_than_stale_is_nobodys():
    a = _agent()
    a.heard("meet:r:p1#0", "Nova, start the post.", True, time.monotonic() - meet.STALE_S - 1, who="meet:r:p1")
    assert a.asker() == ""


def test_the_nudge_wakes_only_on_the_name_opening_an_ask():
    for line in ("I asked Nova.", "Nova's great.", "Did Nova do it?", "Lightbulb moment!", "Novak, can you send it?",
                 "No, Nova.", "Nova.", "Thanks, Nova.", "We should ask Nova about it later.",
                 "Nova is the best.", "Nova did it.", "Nova was great today.", "Nova will handle that later.",
                 "Nova what a day."):  # fmt: skip
        assert not meet.wakes("Nova", line), line
    assert meet.wakes("Nova", "Nova is Saturday free?")  # a question word counts with the question mark
    for line in ("Nova, how many calls did we get today?", "Hey Nova, what's on Wednesday?",
                 "Okay, let's start. Nova, read me the bookings.", "Nova what's on tomorrow?",
                 "light bulb How many phone calls did we get today?", "Lightbulb, please draft the post."):  # fmt: skip
        assert meet.wakes("Nova", line), line


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


def test_no_nudge_when_someone_in_the_room_has_the_assistants_name():
    for people in ([{"name": "Nova Smith"}], [{"name": "Nova"}], [{"name": "Maya"}, {"name": "nova"}]):
        assert not meet.wakes("Nova", "Nova, can you share your screen?", people), people
    assert meet.wakes("Nova", "Nova, can you share your screen?", [{"name": "Maya"}, {"name": "Novak"}])


async def test_call_stats_sends_a_date_range():
    a, sent = _agent(), []

    async def post(op, body):
        sent.append(body)
        return {}

    a._post = post
    await a.call_stats(from_date="2026-10-01", to_date="2026-10-31")
    assert sent == [{"kind": "calls", "days": 7, "from": "2026-10-01", "to": "2026-10-31"}]
