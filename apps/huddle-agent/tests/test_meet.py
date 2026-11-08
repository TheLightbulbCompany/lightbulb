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
    assert out.output == {"sent": True} and sent == []  # it returns at once; the post runs on its own
    await asyncio.gather(*t._posts)
    [(op, body)] = sent
    assert op == "ask" and (body["participantId"], body["speakerName"]) == ("12", "Sam")
    assert body["text"] == "what shipped this week\n\nSaid in the talk since the last ask:\n> Sam: Nova, what shipped this week?"


def test_a_voiced_frame_is_someone_speaking():
    loud = rtc.AudioFrame((np.full(320, 2000, dtype=np.int16)).tobytes(), 16000, 1, 320)
    quiet = rtc.AudioFrame((np.full(320, 10, dtype=np.int16)).tobytes(), 16000, 1, 320)
    assert meet.voiced(loud) and not meet.voiced(quiet)


async def test_a_line_split_at_the_name_is_joined_before_the_gate():
    """8 Nov prod Meet 12:24:45: "Nova, what have we shipped..." at normal pace came as two finals, "Nova." and
    "what have we shipped in the last week?", each gated alone (kind=-): missed."""
    got = []
    j = meet.Joiner(lambda who, text: got.append((who, text)), wait=0.05)
    j.final("maya", "Nova.")
    await asyncio.sleep(0.02)
    j.final("maya", "what have we shipped in the last week?")
    j.final("leo", "Sounds good.")
    await asyncio.sleep(0.1)
    assert got == [("maya", "Nova. what have we shipped in the last week?"), ("leo", "Sounds good.")]
    gate = g()
    assert meet.addressed(gate, "Nova", "maya", got[0][1], 0) == "ask"


async def test_a_long_line_with_pauses_is_one_line():
    """8 Nov prod Meet 12:28:35: a long answer was cut at a pause and only its first part was asked."""
    got = []
    j = meet.Joiner(lambda who, text: got.append(text), wait=0.05)
    j.final("maya", "Nova, for the gel colors, they are nail gels.")
    await asyncio.sleep(0.04)
    j.speaking("maya")  # an interim: still talking, so the line waits for its next final
    await asyncio.sleep(0.1)
    assert got == []
    j.final("maya", "We have none of red. Pink are nearly gone.")
    await asyncio.sleep(0.1)
    assert got == ["Nova, for the gel colors, they are nail gels. We have none of red. Pink are nearly gone."]
    j.speaking("leo")  # nothing pending for them: nothing held
    j.close()


def test_a_handoff_item_has_no_text_for_the_record():
    """8 Nov prod Meet: AttributeError 'AgentHandoff' object has no attribute 'role' on every session start."""
    from livekit.agents.llm import AgentHandoff, ChatMessage

    assert meet.assistant_text(AgentHandoff(new_agent_id="talker")) is None
    assert meet.assistant_text(ChatMessage(role="assistant", content=["We shipped it."])) == "We shipped it."
    assert meet.assistant_text(ChatMessage(role="user", content=["Hi"])) is None
    assert meet.assistant_text(ChatMessage(role="assistant", content=["cut"], interrupted=True)) is None


async def test_the_speaker_is_named_once_in_the_ask():
    """8 Nov prod Meet: "Isol8 Admin said in the meeting: Isol8 Admin: Nova, ..." (the talker's `who:` prefix)."""
    sent = []

    class B:
        async def meet(self, op, body):
            sent.append(body)
            return {}

    t = meet.MeetTalker(B(), {"name": "Nova", "company": "Acme", "title": "Standup", "today": "Sunday"})
    t.asker = ("12", "Isol8 Admin")
    await t.delegate("Isol8 Admin: Nova, what shipped?")
    await asyncio.gather(*t._posts)
    assert sent[0]["text"] == "Nova, what shipped?"


async def test_the_line_after_a_spoken_question_answers_it():
    """8 Nov prod Meet: Pulse asked "What holiday hours?" in a thread; the meeting never heard it, and the spoken
    hours could not answer it. Now the question is said, and the next line asked carries it to be answered."""
    from huddle_agent import mouth

    sent = []

    class B:
        async def meet(self, op, body):
            sent.append(body)
            return {}

    async def fetch(after, handled):
        return {"messages": [{"id": "q1", "seq": 8, "text": "**Pulse** asks: What holiday hours?",
                              "question": {"taskId": "t1", "qid": "q-1"}}], "after": 8}  # fmt: skip

    t = meet.MeetTalker(B(), {"name": "Nova", "company": "Acme", "title": "Standup", "today": "Sunday"})
    t.mouth = m = mouth.Mouth(fetch, 7)
    await m.poll()
    said = []
    m.speak(said.append, ready=True, live=True)
    assert said == [["Pulse asks: What holiday hours?"]]
    await t.delegate("We close at 2 on Christmas Eve.")
    await t.delegate("And what shipped?")
    await asyncio.gather(*t._posts)
    assert sent[0]["answers"] == {"taskId": "t1", "qid": "q-1"} and "answers" not in sent[1]


async def _said_question(t):
    from huddle_agent import mouth

    async def fetch(after, handled):
        return {"messages": [{"id": "q1", "seq": 8, "text": "**Pulse** asks: What holiday hours?",
                              "question": {"taskId": "t1", "qid": "q-1"}}], "after": 8}  # fmt: skip

    t.mouth = m = mouth.Mouth(fetch, 7)
    await m.poll()
    m.speak(lambda texts: None, ready=True, live=True)
    return m


async def test_a_line_long_after_a_spoken_question_is_a_new_ask_not_its_answer():
    """Review of #5112: "Nova, what's on my calendar?" ten minutes later must not become Pulse's answer."""
    from huddle_agent import mouth

    sent = []

    class B:
        async def meet(self, op, body):
            sent.append(body)
            return {}

    t = meet.MeetTalker(B(), {"name": "Nova", "company": "Acme", "title": "Standup", "today": "Sunday"})
    m = await _said_question(t)
    m.question_at -= mouth.ANSWER_WITHIN_S + 1
    await t.delegate("What's on my calendar?")
    await asyncio.gather(*t._posts)
    assert "answers" not in sent[0]
    m = await _said_question(t)  # said again: the next line inside the window answers it
    m.question_at -= mouth.ANSWER_WITHIN_S - 5
    await t.delegate("We close at 2.")
    await asyncio.gather(*t._posts)
    assert sent[1]["answers"] == {"taskId": "t1", "qid": "q-1"} and mouth.ANSWER_WITHIN_S == 60
