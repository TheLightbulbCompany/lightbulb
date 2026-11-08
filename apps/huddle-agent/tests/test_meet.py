"""The meeting's ears and mouth (meet.py): the wake gate, what is posted to the Router, and the 17 scenarios."""

import asyncio

import numpy as np
from livekit import rtc

from huddle_agent import meet
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


def test_the_name_may_open_any_sentence_of_a_joined_turn():
    gate = g()
    text = "Okay, everyone. Let's get started with the check-in. light bulb How many calls did we get?"
    assert meet.Gate(name="Lightbulb", people=PEOPLE).heard("p1", text, 0, new_turn=True) == "ask"
    assert gate.heard("p1", "Leo, did you push it? Nova, what's on tomorrow?", 0, new_turn=True) == "ask"
    assert gate.heard("p2", "We should ask Nova about it later.", 9, new_turn=True) == ""


def test_replay_scenarios_against_the_gate(monkeypatch):
    """The live order: the reply's audio is held until its turn's transcript arrives (TRANSCRIPT_LAG_S). The
    assistant must stay silent where it should, at any lag; it must answer in time (ack_within) at the live lag
    (lag changes how soon, so ack_within is judged only there)."""
    assert len(set(SCENARIOS)) == 17
    live = replay_gate.TRANSCRIPT_LAG_S
    for lag in (0.0, 2.0, live, 8.0):
        monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", lag)
        results = [r for name in SCENARIOS for r in replay_gate.judge(name)]
        assert [r for r in results if r[0] == "FAIL" and r[1] == "silent_between"] == [], lag
    monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", live)
    late = [n for n in SCENARIOS for r in replay_gate.judge(n) if r[:2] == ("FAIL", "ack_within")]
    assert late == []


def test_replay_gate_fails_on_late_ack(monkeypatch):
    monkeypatch.setattr(replay_gate, "TRANSCRIPT_LAG_S", 8.0)  # the transcript lands far too late: acks miss their 4 s
    assert any(r[:2] == ("FAIL", "ack_within") for n in SCENARIOS for r in replay_gate.judge(n))
    assert replay_gate.main() != 0


def test_no_nudge_when_someone_in_the_room_has_the_assistants_name():
    for people in ([{"name": "Nova Smith"}], [{"name": "Nova"}], [{"name": "Maya"}, {"name": "nova"}]):
        assert not meet.wakes("Nova", "Nova, can you share your screen?", people), people
    assert meet.wakes("Nova", "Nova, can you share your screen?", [{"name": "Maya"}, {"name": "Novak"}])


def test_the_name_as_its_own_sentence_then_the_ask_wakes():
    """8 Nov sim: the transcriber wrote "Lightbulb. How many phone calls did we get today?" and the nudge never came,
    so OpenAI (which answers only when asked) said nothing."""
    for line in ("Lightbulb. How many phone calls did we get today?", "Light bulb. How many phone calls today?",
                 "Nova. Read me the bookings.", "Hey Nova. What's on Wednesday?"):  # fmt: skip
        assert meet.wakes("Nova", line), line
    for line in ("Nova. That was great.", "Thanks, Nova. How was your weekend?", "I asked Nova. What did she say?",
                 "Nova. Is it."):  # fmt: skip
        assert not meet.wakes("Nova", line), line




def test_only_an_addressed_line_goes_to_the_router():
    gate = g()
    assert meet.addressed(gate, "Nova", "maya", "Nova, what have we shipped this week?", 0) == "ask"
    for side_talk in ("Leo, did you push the fix?", "I asked Nova what it costs.", "Thanks, Nova.",
                      "We should ask Nova about it later."):  # fmt: skip
        assert meet.addressed(gate, "Nova", "leo", side_talk, 1) == "", side_talk
    assert meet.addressed(gate, "Nova", "maya", "Nova, stop", 2) == "stop"


async def test_the_meeting_talker_has_one_tool_and_answers_only_when_asked(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("LIGHTBULB_API_URL", "http://x")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s")
    from huddle_agent.backend import Backend

    t = meet.MeetTalker(Backend("r", "o", "t"), {"name": "Nova", "company": "C", "title": "T", "today": "d"})
    assert [tool.info.name for tool in t.tools] == ["delegate"]
    td = meet.realtime_model()._opts.turn_detection
    assert td.create_response is False and meet.MODEL == "gpt-realtime-2.1-mini"


async def test_a_meeting_delegation_posts_the_ask_with_its_speaker_and_context():
    sent = []

    class B:
        async def meet(self, op, body):
            await asyncio.sleep(0.01)
            sent.append((op, body))
            return {}

    t = meet.MeetTalker(B(), {"name": "Nova", "company": "Acme", "title": "Standup", "today": "Sunday"})
    t.asker = ("12", "Sam")
    t.context.add("Sam", "Nova, what shipped this week?")
    out = await t.delegate("what shipped this week")
    assert out == {"sent": True} and sent == []  # it returns at once; the post runs on its own
    await asyncio.gather(*t._posts)
    [(op, body)] = sent
    assert op == "ask" and (body["participantId"], body["speakerName"]) == ("12", "Sam")
    assert body["text"] == "what shipped this week\n\nSaid in the talk since the last ask:\n> Sam: Nova, what shipped this week?"


def test_a_voiced_frame_is_someone_speaking():
    loud = rtc.AudioFrame((np.full(320, 2000, dtype=np.int16)).tobytes(), 16000, 1, 320)
    quiet = rtc.AudioFrame((np.full(320, 10, dtype=np.int16)).tobytes(), 16000, 1, 320)
    assert meet.voiced(loud) and not meet.voiced(quiet)
